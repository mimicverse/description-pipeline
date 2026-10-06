"""Adversarial tests for the single-PR publication boundary (local bare remote + mocked GitHub)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.repository import urdf_pr


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="urdf-pr-test-"))
        self.remote = self.tmp / "remote.git"
        run("git", "init", "--bare", "-b", "feature/m3.0", str(self.remote))
        seed = self.tmp / "seed"
        seed.mkdir()
        run("git", "init", "-b", "feature/m3.0", cwd=seed)
        run("git", "config", "user.email", "test@example.com", cwd=seed)
        run("git", "config", "user.name", "Test", cwd=seed)
        (seed / "README.md").write_text("base\n", encoding="utf-8")
        run("git", "add", "-A", cwd=seed)
        run("git", "commit", "-m", "base", cwd=seed)
        run("git", "remote", "add", "origin", str(self.remote), cwd=seed)
        run("git", "push", "-u", "origin", "feature/m3.0", cwd=seed)
        self.repo = self.tmp / "repo"
        run("git", "clone", str(self.remote), str(self.repo))
        run("git", "config", "user.email", "test@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Test", cwd=self.repo)
        self.bundle = self.tmp / "bundle"
        for rel, text in {
            "README.md": "# m3.0\n",
            "input/robot.yaml": "hardware: m3.0\n",
            "evidence/raw.json": "{}\n",
            "model/robot.json": "{}\n",
            "urdf/robot.urdf": "<robot name='m3.0'/>\n",
            "meshes/part.stl": "solid\n",
            "reports/input.json": "{}\n",
            "reports/tool.json": "{}\n",
        }.items():
            path = self.bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def remote_branch_sha(self, branch: str = "work/solidworks/m3.0") -> str:
        out = run("git", "ls-remote", str(self.remote), f"refs/heads/{branch}").strip()
        return out.split()[0] if out else ""


class SubmitBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.gh_calls: list[tuple[str, ...]] = []
        self.pr_exists = False

        def fake_gh(*args: str) -> str:
            self.gh_calls.append(args)
            if args[:2] == ("pr", "list"):
                return json.dumps([{"url": "https://example.test/pr/1", "number": 1}]) if self.pr_exists else "[]"
            if args[:2] == ("pr", "create"):
                self.pr_exists = True
                return "https://example.test/pr/1\n"
            raise AssertionError(args)

        def verifier(bundle: Path) -> dict:
            report = {"passed": True, "subject_sha256": urdf_pr.subject_hash(bundle)}
            (bundle / "reports/quality.json").write_text(urdf_pr._serialize(report), encoding="utf-8")
            return report

        self.patchers = [mock.patch.object(urdf_pr, "_gh", fake_gh),
                         mock.patch.object(urdf_pr, "_load_verifier", lambda: verifier)]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

    def submit(self, **kwargs):
        return urdf_pr.submit_bundle(self.fx.bundle, self.fx.repo, base="feature/m3.0",
                                     branch="work/solidworks/m3.0", **kwargs)

    def test_valid_publish_then_reuse_one_pr(self) -> None:
        first = self.submit()
        self.assertIn(first["state"], {"published", "reused"})
        self.assertEqual(first["url"], "https://example.test/pr/1")
        self.assertEqual(self.fx.remote_branch_sha(), first["commit"])
        second = self.submit()
        self.assertEqual(second["state"], "reused")
        self.assertEqual(second["url"], first["url"])
        self.assertEqual(len([c for c in self.gh_calls if c[:2] == ("pr", "create")]), 1)
        self.assertEqual(self.fx.remote_branch_sha(), second["commit"])

    def test_tampered_subject_never_pushes(self) -> None:
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: (lambda bundle: {"passed": True, "subject_sha256": "0" * 64})):
            result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "verification_subject_mismatch")
        self.assertEqual(self.fx.remote_branch_sha(), "")

    def test_stale_passed_report_never_pushes(self) -> None:
        (self.fx.bundle / "reports/quality.json").write_text('{"passed": true, "subject_sha256": "old"}\n', encoding="utf-8")
        no_write = lambda bundle: {"passed": True, "subject_sha256": urdf_pr.subject_hash(bundle)}  # noqa: E731
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: no_write):
            result = self.submit()
        self.assertEqual(result["error"], "stale_quality_report")
        self.assertEqual(self.fx.remote_branch_sha(), "")

    def test_dirty_repository_preserved(self) -> None:
        user_file = self.fx.repo / "user-notes.txt"
        user_file.write_text("mine\n", encoding="utf-8")
        result = self.submit()
        self.assertEqual(result["error"], "dirty_repository")
        self.assertTrue(user_file.is_file())
        self.assertEqual(self.fx.remote_branch_sha(), "")

    def test_failed_verifier_never_pushes(self) -> None:
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: (lambda bundle: {"passed": False})):
            result = self.submit()
        self.assertEqual(result["error"], "verification_failed")
        self.assertEqual(self.fx.remote_branch_sha(), "")

    def test_bad_layout_and_branch(self) -> None:
        (self.fx.bundle / "urdf/robot.urdf").unlink()
        self.assertEqual(self.submit()["error"], "bundle_incomplete")
        (self.fx.bundle / "urdf/robot.urdf").write_text("<robot/>\n", encoding="utf-8")
        bad = urdf_pr.submit_bundle(self.fx.bundle, self.fx.repo, base="feature/m3.0", branch="work/other")
        self.assertEqual(bad["error"], "branch_not_deterministic")

    def test_dry_run_does_not_push(self) -> None:
        result = self.submit(dry_run=True)
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(self.fx.remote_branch_sha(), "")


if __name__ == "__main__":
    unittest.main()
