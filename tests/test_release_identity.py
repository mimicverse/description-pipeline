"""Nothing a release carries may repeat the captured document's real identity.

An sdist ships ``tests/*.py`` and none of ``tests/fixtures/**``, so a real Onshape document,
workspace or element id copied into a test file is published by ``pip download --no-binary`` (the
0.3.14 sdist did exactly that).  The fixture stays the only place that holds them, the test files
read them from ``source.json``, and this guard fails when one comes back — with a control proving the
ids it looks for are real, so it cannot pass by looking for nothing.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "onshape"
SOURCE = FIXTURES / "cache" / "source.json"
#: A 24-hex id, but not a window of a longer hash and not part of a longer name run.
HEX24 = re.compile(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
#: Everything an archive can carry: the sdist holds src/**, tests/*.py and the top-level metadata,
#: and the wheel and the bundles add tools/, docs/ and examples/ through their own packaging.
SKIP = {".git", ".venv", "__pycache__", "build", "dist"}


def shipped_files() -> list[Path]:
    found: list[Path] = []
    for directory in ("src", "tools", "docs", "examples", "tests"):
        for path in sorted((ROOT / directory).rglob("*")):
            if not path.is_file() or any(part in SKIP for part in path.relative_to(ROOT).parts):
                continue
            if path.is_relative_to(FIXTURES):
                continue
            if directory == "tests" and path.suffix not in {".py", ".md", ".json", ".yaml", ".yml", ".cfg", ".toml"}:
                continue
            found.append(path)
    for name in ("README.md", "README.en.md", "pyproject.toml", "setup.cfg", "CHANGELOG.md"):
        if (ROOT / name).is_file():
            found.append(ROOT / name)
    return found


def fixture_identity() -> set[str]:
    """Every id the capture recorded: the source record plus the names of its cached responses."""

    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    found = {value for key, value in source.items() if key.endswith("_id") and isinstance(value, str)}
    found.update(HEX24.findall(str(source.get("url", ""))))
    for path in FIXTURES.rglob("*"):
        found.update(HEX24.findall(path.name))
    return {value for value in found if HEX24.fullmatch(value)}


@unittest.skipUnless(SOURCE.is_file(), "the fixture is absent from a source archive")
class FixtureIdentityTests(unittest.TestCase):
    def test_the_fixture_still_carries_the_identity(self):
        """Without this the guard below could pass by comparing against nothing."""

        identifiers = fixture_identity()
        self.assertGreaterEqual(len(identifiers), 4, identifiers)
        recorded = SOURCE.read_text(encoding="utf-8") + " ".join(path.name for path in FIXTURES.rglob("*"))
        for value in identifiers:
            self.assertIn(value, recorded, f"{value} is not in the fixture after all")

    def test_no_shippable_file_repeats_the_identity(self):
        identifiers = fixture_identity()
        names = sorted(shipped_files())
        self.assertGreater(len(names), 50, "the shipped-file scan found too little to be meaningful")
        offenders: list[str] = []
        for path in names:
            text = path.read_text(encoding="utf-8", errors="ignore")
            offenders.extend(
                f"{path.relative_to(ROOT).as_posix()}: {value}" for value in sorted(identifiers) if value in text
            )
        self.assertEqual(offenders, [], "夹具身份不能出现在任何会随发行版发布的文件里")


if __name__ == "__main__":
    unittest.main()
