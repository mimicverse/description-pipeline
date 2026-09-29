"""``description quickstart`` must hand a new user a workspace that already works.

The command exists because a release bundle carries no examples: without the packaged copy the
documented "try it without CAD" path starts with a git clone.  These tests keep that copy identical
to ``examples/demo-arm``, prove the scaffolded workspace builds and qualifies, and pin the two
refusals a first-time user is most likely to hit.
"""

import glob
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path, PurePosixPath

from description_pipeline.io import PipelineError
from description_pipeline.quickstart import PACKAGE_DEMO, run, scaffold
from description_pipeline.repository import check_layout

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/description_pipeline"
EXAMPLE = ROOT / "examples/demo-arm"
TEMPLATE = PACKAGE / PACKAGE_DEMO
CLI = [sys.executable, "-m", "description_pipeline"]

#: The packaging step cannot carry hidden files, so these two travel without their dot.
TEMPLATE_NAMES = {".gitignore": "gitignore", ".gitattributes": "gitattributes"}
IGNORED = {"build", ".venv", "__pycache__"}


def snapshot(root: Path, translate: dict[str, str] | None = None) -> dict[str, bytes]:
    """Every file under ``root`` keyed by its relative path, in the packaged naming."""

    translate = translate or {}
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in IGNORED:
            continue
        parts = [translate.get(part, part) for part in relative.parts]
        found[PurePosixPath(*parts).as_posix()] = path.read_bytes()
    return found


@unittest.skipUnless(EXAMPLE.is_dir(), "the repository example is absent from a source archive")
class PackagedDemoTests(unittest.TestCase):
    def test_packaged_demo_matches_the_repository_example(self):
        self.assertEqual(
            snapshot(TEMPLATE),
            snapshot(EXAMPLE, TEMPLATE_NAMES),
            "examples/demo-arm and the packaged quickstart copy are the same workspace; update both",
        )

    def test_package_data_patterns_ship_every_demo_file(self):
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"][
            "package-data"
        ]["description_pipeline"]
        shipped: set[str] = set()
        for pattern in declared:
            shipped.update(
                Path(found).relative_to(PACKAGE).as_posix()
                for found in glob.glob(str(PACKAGE / pattern), recursive=True)
                if Path(found).is_file()
            )
        missing = sorted({f"{PACKAGE_DEMO}/{name}" for name in snapshot(TEMPLATE)} - shipped)
        self.assertEqual(missing, [], "package-data patterns must carry the whole demo workspace")


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_writes_the_demo_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "demo"
            value = scaffold(target)
            self.assertEqual(value["files"], len(snapshot(TEMPLATE)))
            self.assertTrue((target / ".gitignore").is_file())
            self.assertTrue((target / ".gitattributes").is_file())
            self.assertTrue(check_layout(target, "model")["passed"])

    def test_scaffold_refuses_a_used_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "demo"
            target.mkdir()
            keep = target / "robot.yaml"
            keep.write_text("mine", encoding="utf-8")
            with self.assertRaises(PipelineError) as raised:
                scaffold(target)
            self.assertIn("not empty", str(raised.exception))
            self.assertEqual([path.name for path in target.iterdir()], ["robot.yaml"])
            self.assertEqual(keep.read_text(encoding="utf-8"), "mine")

    def test_scaffolded_workspace_builds_and_qualifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "demo"
            scaffold(target)
            value = run(target)
            self.assertTrue(value["passed"], value["blockers"])
            self.assertEqual(value["qualified_for"], ["kinematics"])
            self.assertTrue(Path(value["quality"]).is_file())


class QuickstartCliTests(unittest.TestCase):
    def test_quickstart_prints_the_next_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [*CLI, "quickstart", "demo"], cwd=temporary, capture_output=True, text=True, encoding="utf-8"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["root"], "demo")
            self.assertIn("next: cd demo", result.stderr)
            self.assertIn("next: description check --root . --profile kinematics", result.stderr)
            self.assertTrue((Path(temporary) / "demo" / "config" / "robot.yaml").is_file())

    def test_quickstart_answers_a_used_directory_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "keep.txt").write_text("mine", encoding="utf-8")
            result = subprocess.run(
                [*CLI, "quickstart", "."], cwd=temporary, capture_output=True, text=True, encoding="utf-8"
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("not empty", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(sorted(path.name for path in Path(temporary).iterdir()), ["keep.txt"])


if __name__ == "__main__":
    unittest.main()
