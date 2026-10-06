"""Real local bare-Git tests for the fast-forward single-PR publication boundary."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import subject_digest
from description_pipeline.repository import urdf_pr

BRANCH = "work/solidworks/m3.0"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Fixture:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="urdf-pr-test-"))
        self.remote = self.tmp / "remote.git"
        run("git", "init", "--bare", "-b", "feature/m3.0", str(self.remote))
        self.seed = self.tmp / "seed"
        self.seed.mkdir()
        run("git", "init", "-b", "feature/m3.0", cwd=self.seed)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            run("git", "config", key, value, cwd=self.seed)
        (self.seed / "README.md").write_text("base\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.seed)
        run("git", "commit", "-m", "base", cwd=self.seed)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.seed)
        run("git", "push", "-u", "origin", "feature/m3.0", cwd=self.seed)
        self.repo = self.tmp / "repo"
        run("git", "clone", str(self.remote), str(self.repo))
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            run("git", "config", key, value, cwd=self.repo)
        self.bundle = self.tmp / "bundle"
        self.write_bundle("one\n")

    def write_bundle(self, evidence: str, extra: dict[str, str] | None = None) -> None:
        files = {
            "README.md": "# m3.0\n",
            "input/robot.yaml": "hardware_id: m3.0\n",
            "evidence/raw.json": evidence,
            "model/robot.json": "{}\n",
            "urdf/robot.urdf": "<robot name='m3.0'/>\n",
            "meshes/part.stl": "solid\n",
            "reports/input.json": "{}\n",
            "reports/tool.json": "{}\n",
        }
        files.update(extra or {})
        for rel, text in files.items():
            path = self.bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def remote_head(self, branch: str = BRANCH) -> str:
        out = run("git", "ls-remote", str(self.remote), f"refs/heads/{branch}").strip()
        return out.split()[0] if out else ""

    def remote_tree(self, branch: str = BRANCH) -> list[str]:
        run("git", "fetch", "--quiet", "origin", branch, cwd=self.repo)
        return run("git", "ls-tree", "-r", "--name-only", "FETCH_HEAD", cwd=self.repo).splitlines()

    def is_ancestor(self, older: str, newer: str) -> bool:
        return subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", older, newer],
                              capture_output=True).returncode == 0


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.gh_calls: list[tuple[str, ...]] = []
        self.gh_mode = "ok"
        self.pr_exists = False

        def fake_gh(repository: Path, *args: str) -> str:
            self.gh_calls.append(args)
            if self.gh_mode == "fail":
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh down")
            if args[:2] == ("pr", "list"):
                if self.pr_exists:
                    return json.dumps([{"url": "https://example.test/pr/1", "number": 1,
                                        "baseRefName": "feature/m3.0", "headRefName": BRANCH}])
                return "[]"
            if args[:2] == ("pr", "create"):
                self.pr_exists = True
                return "https://example.test/pr/1\n"
            if args[:2] == ("pr", "edit"):
                return ""
            raise AssertionError(args)

        def verifier(bundle: Path) -> dict:
            report = {"passed": True, "subject_sha256": subject_digest(bundle), "checks": [{"id": "x", "status": "passed"}]}
            (bundle / "reports/quality.json").write_text(urdf_pr._serialize(report), encoding="utf-8")
            return report

        self.patchers = [mock.patch.object(urdf_pr, "_gh", fake_gh),
                         mock.patch.object(urdf_pr, "_load_verifier", lambda: verifier),
                         mock.patch.object(urdf_pr, "_origin_slug", lambda repository: "example/m3.0")]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

    def submit(self, **kwargs):
        return urdf_pr.submit_bundle(self.fx.bundle, self.fx.repo, base="feature/m3.0", branch=BRANCH, **kwargs)

    def test_repeat_runs_fast_forward_then_noop(self) -> None:
        base = self.fx.remote_head("feature/m3.0")
        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        self.assertEqual(self.fx.remote_head(), first["commit"])
        self.assertTrue(self.fx.is_ancestor(base, first["commit"]))
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))
        self.assertEqual(self.fx.remote_head(), second["commit"])
        third = self.submit()
        self.assertEqual(third["state"], "noop")
        self.assertEqual(third["commit"], second["commit"])
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertEqual(len([c for c in self.gh_calls if c[:2] == ("pr", "create")]), 1)
        self.assertGreaterEqual(len([c for c in self.gh_calls if c[:2] == ("pr", "edit")]), 2)

    def test_base_advance_merges_preserving_ancestry(self) -> None:
        first = self.submit()
        (self.fx.seed / "feature.txt").write_text("feature\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "advance base", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        run("git", "fetch", "--quiet", "origin", "feature/m3.0", cwd=self.fx.repo)
        new_base = run("git", "rev-parse", "FETCH_HEAD", cwd=self.fx.repo).strip()
        self.fx.write_bundle("three\n")
        second = self.submit()
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))
        self.assertTrue(self.fx.is_ancestor(new_base, second["commit"]))

    def test_dirty_checkout_preserved(self) -> None:
        user_file = self.fx.repo / "user-notes.txt"
        user_file.write_text("mine\n", encoding="utf-8")
        result = self.submit()
        self.assertEqual(result["error"], "dirty_repository")
        self.assertTrue(user_file.is_file())
        self.assertEqual(self.fx.remote_head(), "")

    def test_foreign_branch_refused_without_overwrite(self) -> None:
        (self.fx.seed / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "foreign branch", cwd=self.fx.seed)
        run("git", "push", "origin", "HEAD:refs/heads/" + BRANCH, cwd=self.fx.seed)
        foreign = self.fx.remote_head()
        result = self.submit()
        self.assertEqual(result["error"], "branch_foreign")
        self.assertEqual(self.fx.remote_head(), foreign)

    def test_bad_verification_and_tamper_never_push(self) -> None:
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: (lambda bundle: {"passed": False})):
            self.assertEqual(self.submit()["error"], "verification_failed")
        self.assertEqual(self.fx.remote_head(), "")
        with mock.patch.object(urdf_pr, "_load_verifier",
                               lambda: (lambda bundle: {"passed": True, "subject_sha256": "0" * 64})):
            self.assertEqual(self.submit()["error"], "verification_subject_mismatch")
        self.assertEqual(self.fx.remote_head(), "")
        good = {"passed": True, "subject_sha256": subject_digest(self.fx.bundle)}
        (self.fx.bundle / "reports/quality.json").write_text('{"passed": true}\n', encoding="utf-8")
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: (lambda bundle: good)):
            self.assertEqual(self.submit()["error"], "stale_quality_report")
        self.assertEqual(self.fx.remote_head(), "")

    def test_gh_failure_after_push_keeps_pushed_receipt(self) -> None:
        self.gh_mode = "fail"
        result = self.submit()
        self.assertEqual(result["state"], "gh_failed_after_push")
        self.assertEqual(result["commit"], self.fx.remote_head())
        self.assertEqual(result["branch"], BRANCH)
        self.assertTrue(result["subject"])
        self.assertIn("retry", result)

    def test_ungoverned_files_never_reach_the_branch(self) -> None:
        self.fx.write_bundle("four\n", extra={"secret.txt": "shh\n", "config/extra.json": "{}\n"})
        result = self.submit()
        self.assertIn(result["state"], {"published", "updated"})
        tree = self.fx.remote_tree()
        self.assertNotIn("secret.txt", tree)
        self.assertNotIn("config/extra.json", tree)
        self.assertIn("urdf/robot.urdf", tree)

    def test_local_receipts_ignored_and_do_not_cause_commits(self) -> None:
        self.fx.write_bundle("one\n", extra={"reports/run.json": '{"run": 1}\n',
                                            "reports/pr.json": '{"pr": 1}\n'})
        first = self.submit()
        tree = self.fx.remote_tree()
        self.assertIn("reports/quality.json", tree)
        self.assertNotIn("reports/run.json", tree)
        self.assertNotIn("reports/pr.json", tree)
        self.fx.write_bundle("one\n", extra={"reports/run.json": '{"run": 2}\n',
                                            "reports/pr.json": '{"pr": 2}\n'})
        second = self.submit()
        self.assertEqual(second["state"], "noop")
        self.assertEqual(second["commit"], first["commit"])
        self.assertEqual(self.fx.remote_head(), first["commit"])

    def test_base_advance_with_identical_bundle_pushes_merge(self) -> None:
        first = self.submit()
        (self.fx.seed / "feature.txt").write_text("feature\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "advance base", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        run("git", "fetch", "--quiet", "origin", "feature/m3.0", cwd=self.fx.repo)
        new_base = run("git", "rev-parse", "FETCH_HEAD", cwd=self.fx.repo).strip()
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertNotEqual(second["commit"], first["commit"])
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))
        self.assertTrue(self.fx.is_ancestor(new_base, second["commit"]))

    def test_invalid_refs_rejected(self) -> None:
        result = urdf_pr.submit_bundle(self.fx.bundle, self.fx.repo, base="--upload-pack=evil", branch=BRANCH)
        self.assertEqual(result["error"], "invalid_ref")
        self.assertEqual(self.fx.remote_head(), "")

    def test_case_duplicate_paths_rejected(self) -> None:
        (self.fx.bundle / "readme.md").write_text("dup\n", encoding="utf-8")
        self.assertEqual(self.submit()["error"], "duplicate_path")
        self.assertEqual(self.fx.remote_head(), "")

    def test_inherited_symlink_not_followed(self) -> None:
        outside = self.fx.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        os.symlink(outside, self.fx.seed / "input")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "symlinked input", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        result = self.submit()
        self.assertIn(result["state"], {"published", "updated"})
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertIn("input/robot.yaml", self.fx.remote_tree())

    def test_commit_hook_tamper_blocks_push(self) -> None:
        hook = self.fx.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('#!/bin/sh\necho tampered >> "$(git rev-parse --show-toplevel)/urdf/robot.urdf"\n',
                        encoding="utf-8")
        hook.chmod(0o755)
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertIn(result["error"], {"commit_left_dirty", "committed_subject_mismatch",
                                        "reverification_failed", "reverification_binding_mismatch"})
        self.assertEqual(self.fx.remote_head(), "")


if __name__ == "__main__":
    unittest.main()
