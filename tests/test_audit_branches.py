"""The branch-level contract sweep: one command answers "which model branches are clean?".

That question used to be answered by hand, one temporary worktree at a time — and when it was finally
asked, two branches answered with 50 and 51 errors nobody had seen.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_urdf_quality import AuditCase, correct_urdf, joint, link, urdf

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "audit_branches.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("entry_audit_branches", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["entry_audit_branches"] = module
    spec.loader.exec_module(module)
    return module


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, text=True, encoding="utf-8", check=True
    )


def zero_limits_urdf() -> str:
    """A model whose joints declare effort=0 velocity=0 — the defect two real branches carried."""

    return urdf(
        links=link("base_link") + link("arm_link", mass="0.2"),
        joints=joint("base_arm_joint", limits="-1 1 0 0"),
    )


class BranchAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.remote = self.base / "remote.git"
        self.repo = self.base / "repo"
        git(self.base, "init", "--bare", str(self.remote))
        git(self.base, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "README.md").write_text("tooling\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "tooling base")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "origin", "main")

    def _branch(self, name: str, build) -> None:
        git(self.repo, "checkout", "-q", "-b", name, "main")
        build(self.repo)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", f"model {name}")
        git(self.repo, "push", "-q", "origin", name)
        git(self.repo, "checkout", "-q", "main")

    def model_branch(self, name: str, urdf_text: str) -> None:
        self._branch(name, lambda root: AuditCase(root, urdf_text, joint_names=["base_arm_joint"]))

    def test_the_sweep_reports_every_model_branch_and_cleans_up(self) -> None:
        self.model_branch("feature/clean", correct_urdf())
        self.model_branch("feature/broken", zero_limits_urdf())
        self.model_branch("release/clean-too", correct_urdf())
        self._branch("feature/no-model", lambda root: (root / "notes.md").write_text("no model\n", encoding="utf-8"))

        report = self.base / "branches.json"
        code = load_tool().main(["--repository", str(self.repo), "--json", str(report)])
        self.assertEqual(code, 1, "a broken branch must fail the sweep")
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(data["passed"])
        branches = {item["branch"]: item for item in data["branches"]}
        self.assertEqual(sorted(branches), ["feature/broken", "feature/clean", "release/clean-too"])
        self.assertTrue(branches["feature/clean"]["passed"])
        self.assertFalse(branches["feature/broken"]["passed"])
        self.assertEqual(branches["feature/broken"]["error"], 2)
        self.assertEqual(branches["feature/broken"]["codes"]["URDF202"], 2)
        self.assertEqual(
            branches["feature/broken"]["commit"], git(self.repo, "rev-parse", "feature/broken").stdout.strip()
        )
        leftover = git(self.repo, "worktree", "list", "--porcelain").stdout.count("worktree ")
        self.assertEqual(leftover, 1, "the sweep must remove every temporary worktree")

    def test_a_prefix_narrows_the_sweep_and_an_empty_selection_passes(self) -> None:
        self.model_branch("feature/clean", correct_urdf())
        self.model_branch("release/clean-too", correct_urdf())

        report = self.base / "feature-only.json"
        tool = load_tool()
        self.assertEqual(tool.main(["--repository", str(self.repo), "--ref", "feature/", "--json", str(report)]), 0)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual([item["branch"] for item in data["branches"]], ["feature/clean"])
        self.assertTrue(data["passed"])

        self.assertEqual(tool.main(["--repository", str(self.repo), "--ref", "nothing/"]), 0)
