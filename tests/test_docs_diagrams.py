"""Mermaid diagrams in the documents must stay renderable.

A broken diagram is invisible to every text check: the file still parses, the links still resolve,
and the reader sees an error box.  The cheap half of this test always runs (every block must declare a
known diagram type); the deep half renders each diagram with mermaid-cli when it and a Chrome build
are available locally, and skips with a reason otherwise.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAM_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
)
CHROME_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def documents() -> list[Path]:
    found = [ROOT / "README.md", ROOT / "README.en.md"]
    found += sorted((ROOT / "docs").rglob("*.md"))
    return [path for path in found if path.is_file()]


def diagrams() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in documents():
        text = path.read_text(encoding="utf-8")
        for body in re.findall(r"```mermaid\n(.*?)```", text, re.S):
            found.append((path, body.strip("\n")))
    return found


def chrome_path() -> str | None:
    from_env = os.environ.get("MERMAID_CHROME")
    if from_env and Path(from_env).is_file():
        return from_env
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium")


class DiagramTests(unittest.TestCase):
    def test_documents_contain_diagrams(self):
        self.assertGreaterEqual(len(diagrams()), 1, "the runbook documents the data flow as a diagram")

    def test_every_diagram_declares_a_known_type(self):
        for path, body in diagrams():
            with self.subTest(document=path.relative_to(ROOT).as_posix()):
                first = body.splitlines()[0].strip()
                self.assertTrue(
                    first.startswith(DIAGRAM_TYPES),
                    f"unknown Mermaid diagram type: {first!r}",
                )

    def test_diagrams_render(self):
        mmdc = shutil.which("mmdc")
        chrome = chrome_path()
        if mmdc is None or chrome is None:
            self.skipTest("mermaid-cli and a Chrome build are needed to render diagrams")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            config = work / "puppeteer.json"
            config.write_text(
                '{"executablePath": "' + chrome + '", "args": ["--no-sandbox", "--disable-gpu"]}',
                encoding="utf-8",
            )
            for path, body in diagrams():
                with self.subTest(document=path.relative_to(ROOT).as_posix()):
                    source = work / (path.stem + ".mmd")
                    target = work / (path.stem + ".svg")
                    source.write_text(body + "\n", encoding="utf-8", newline="\n")
                    result = subprocess.run(
                        [mmdc, "-p", str(config), "-i", str(source), "-o", str(target)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
                    self.assertGreater(target.stat().st_size, 1000, "the rendered diagram looks empty")


if __name__ == "__main__":
    unittest.main()
