"""A review starts with an answer, not with the megabytes of JSON that answer sits in.

Comparing two real candidates printed 2.5 MB and 67,600 lines of JSON on stdout and nothing on
stderr, so the reviewer's first question - did anything change? - could only be answered by reading
the whole report.  ``description diff`` now prints one sentence on stderr before the report,
``summary`` carries the same answer for machines, and ``--json`` leaves stderr empty for callers
that treat stderr as noise.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from description_pipeline import cli

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "demo-arm"
GIT = shutil.which("git")


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Keep the two streams apart: stdout is the report, stderr is the sentence."""

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(GIT, "the diff check resolves revisions through Git")
class DiffSummaryTests(unittest.TestCase):
    """The shipped example is a built candidate, and diff reads candidates instead of building them."""

    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    repository: Path
    before: Path
    after: Path

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.repository = cls.root / "repository"
        cls.repository.mkdir()
        subprocess.run([GIT or "git", "init", "--quiet", str(cls.repository)], check=True, capture_output=True)
        cls.before, cls.after = cls.root / "before", cls.root / "after"
        shutil.copytree(EXAMPLE, cls.before)
        shutil.copytree(EXAMPLE, cls.after)
        definition = cls.after / "config" / "robot.yaml"
        definition.write_text(
            definition.read_text(encoding="utf-8").replace('"demo-arm"', '"revised-demo-arm"'), encoding="utf-8"
        )

    def diff(self, before: Path, after: Path, *flags: str) -> tuple[int, str, str]:
        return run_cli(["diff", *flags, "--repository", str(self.repository), str(before), str(after)])

    def test_identical_candidates_are_one_sentence_on_stderr(self):
        code, report, sentence = self.diff(self.before, self.before)
        self.assertEqual(code, 0, sentence)
        value = json.loads(report)
        self.assertFalse(value["summary"]["changed"])
        self.assertEqual(value["summary"]["changed_areas"], [])
        self.assertFalse(value["summary"]["subject_changed"])
        self.assertEqual(len(sentence.splitlines()), 1, sentence)
        self.assertTrue(sentence.startswith(f"diff: {self.before} -> {self.before}: no changes;"), sentence)
        self.assertIn(f"subject {value['before'][:12]} unchanged", sentence)

    def test_the_sentence_names_what_changed_and_json_silences_it(self):
        code, report, sentence = self.diff(self.before, self.after)
        self.assertEqual(code, 0, sentence)
        value = json.loads(report)
        self.assertEqual(value["summary"]["changed_areas"], ["config/robot.yaml"])
        self.assertIn("1 area changed", sentence)
        self.assertIn("config/robot.yaml: 1 field", sentence)
        self.assertIn(f"subject {value['before'][:12]} -> {value['after'][:12]}", sentence)
        quiet = self.diff(self.before, self.after, "--json")
        self.assertEqual(quiet, (0, report, ""))

    def test_a_long_report_stays_one_readable_line(self):
        """Six changed areas still fit one line, and a category nothing changed in is not named."""

        value = {
            "summary": {
                "changed": True,
                "changed_areas": ["links", "config/robot.yaml", "profile", "provenance", "control", "qualified_for"],
                "robot_objects": {"added": 0, "removed": 1, "modified": 1},
                "subject_changed": True,
            },
            "before": "a" * 64,
            "after": "b" * 64,
            "references": [{"commit": "c" * 40}, {"directory": "/models/robot"}],
            "changes": {
                "links": {"added": [], "removed": ["arm_link"], "modified": [{"id": "base_link"}]},
                "joints": {"added": [], "removed": [], "modified": []},
                "config/robot.yaml": [{"path": "/hardware_id"}, {"path": "/overrides"}],
                "profile": {"before": {"purpose": "kinematics"}, "after": {"purpose": "simulation"}},
                "provenance": {"before": 1, "after": 2},
                "control": {"before": 1, "after": 2},
                "qualified_for": {"before": ["kinematics"], "after": []},
            },
        }
        sentence = cli._diff_line(value)
        self.assertEqual(len(sentence.splitlines()), 1, sentence)
        self.assertIn("cccccccccccc -> /models/robot: 6 areas changed (", sentence)
        self.assertIn("links: 1 removed + 1 modified", sentence)
        self.assertIn("config/robot.yaml: 2 fields", sentence)
        self.assertIn("+1 more", sentence)
        self.assertIn("subject aaaaaaaaaaaa -> bbbbbbbbbbbb", sentence)
        self.assertNotIn("joints", sentence)

    def test_a_delivery_that_moved_without_compared_changes_says_so(self):
        """An uncompared delivered file changes the digest; the sentence may not claim "no changes"."""

        value = {
            "summary": {
                "changed": False,
                "changed_areas": [],
                "robot_objects": {"added": 0, "removed": 0, "modified": 0},
                "subject_changed": True,
            },
            "before": "a" * 64,
            "after": "b" * 64,
            "references": [{"directory": "/models/before"}, {"directory": "/models/after"}],
            "changes": {"links": {"added": [], "removed": [], "modified": []}},
        }
        sentence = cli._diff_line(value)
        self.assertEqual(len(sentence.splitlines()), 1, sentence)
        self.assertIn("/models/before -> /models/after: no changes in the compared areas", sentence)
        self.assertIn("delivery digest moved (aaaaaaaaaaaa -> bbbbbbbbbbbb)", sentence)


if __name__ == "__main__":
    unittest.main()
