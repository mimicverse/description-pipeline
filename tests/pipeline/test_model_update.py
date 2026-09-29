# ``description model update`` preflight/ordering and the submit review contract.
#
# Everything here drives the real orchestration functions with the git/GitHub side
# doubled, so the tests pin *when* each stage runs and what a failed central dispatch
# is allowed to report.

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from description_pipeline import cli
from description_pipeline.io import PipelineError
from description_pipeline.repository import _verify_github_cli, submit, update

REPORT = {"passed": True, "subject": "a" * 64, "profile_digest": "b" * 64, "blockers": [], "hardware_id": "microban"}


def git_stub(
    branch: str = "work/model/microban/change",
    sha: str = "c" * 40,
    status: str = "",
    feature_exists: bool = True,
    base_contained: bool = True,
    fetch_fails: bool = False,
    merge_base_error: bool = False,
):
    """Minimal ``git`` double: only the calls the update path makes."""

    def run(root: Path, *args: str, check: bool = True):
        if args[:2] == ("branch", "--show-current"):
            out = branch
        elif args[:2] == ("merge-base", "--is-ancestor"):
            # ``origin/feature/<hardware>`` either is contained in the review branch or
            # moved on since it was cut; nothing else is asked here.
            code = 128 if merge_base_error else (0 if base_contained else 1)
            return subprocess.CompletedProcess(args, code, "", "")
        elif args and args[0] == "fetch":
            # A failed fetch leaves the previous remote-tracking ref in place; the caller must not
            # read it as the current remote state.
            return subprocess.CompletedProcess(args, 1 if fetch_fails else 0, "", "")
        elif args and args[0] == "ls-remote":
            out = f"{sha}\t{args[2]}" if (feature_exists and args[2].endswith("feature/microban")) else ""
        elif args[0] == "status":
            out = status
        elif args[:2] == ("rev-parse", "HEAD"):
            out = sha
        elif args[:2] == ("rev-parse", "--git-path"):
            out = f"{root}/.git/description-update.lock"
        elif args[:2] == ("rev-parse", "--verify") or args[0] == "diff":
            if any(argument.startswith("refs/remotes/origin/feature/") for argument in args):
                return subprocess.CompletedProcess(args, 0 if feature_exists else 1, f"{sha}\n", "")
            return subprocess.CompletedProcess(args, 1, "", "")
        else:
            out = ""
        return subprocess.CompletedProcess(args, 0, out, "")

    return run


class ModelUpdateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "model"
        (self.root / ".git").mkdir(parents=True)

    def _prepare(
        self,
        *,
        branch="work/model/microban/change",
        status="",
        toolchain=None,
        worker="http://127.0.0.1:8765",
        feature_exists=True,
        profile_for=None,
    ):
        order: list[str] = []

        def freeze_stub(root):
            order.append("freeze")
            return {"snapshot": "sources/snapshots/abc"}

        def build_stub(root, profile):
            order.append("build")
            return dict(REPORT)

        def submit_stub(root, profile, message, *, ci=False):
            order.append("submit")
            if not (root / ".git/description-update.lock").is_file():
                raise AssertionError("update must hold the workspace lock while submitting")
            return {"passed": True, "state": "pull_request_open", "pull_request": "https://example/pr/1"}

        patches = [
            patch("description_pipeline.repository.check_layout", return_value={"role": "model", "passed": True}),
            patch(
                "description_pipeline.repository.git",
                side_effect=git_stub(branch=branch, status=status, feature_exists=feature_exists),
            ),
            patch("description_pipeline.repository.repository_slug", return_value="org/repo"),
            patch(
                "description_pipeline.repository.verify_toolchain",
                side_effect=toolchain or (lambda root: {"version": "0.3.2", "source_commit": "d" * 40}),
            ),
            patch(
                "description_pipeline.repository.profile_for",
                side_effect=profile_for or (lambda root, profile: {"purpose": "simulation"}),
            ),
            patch(
                "description_pipeline.repository.definition",
                return_value={"hardware_id": "microban", "source": {"worker_url": worker}},
            ),
            patch("description_pipeline.repository.freeze", side_effect=freeze_stub),
            patch("description_pipeline.repository.build", side_effect=build_stub),
            patch("description_pipeline.repository._submit", side_effect=submit_stub),
            patch(
                "description_pipeline.repository._verify_github_cli",
                return_value={"installed": True, "authenticated": True},
            ),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return order


class ModelUpdateTests(ModelUpdateFixture):
    def test_stages_run_in_order_under_the_workspace_lock(self):
        order = self._prepare()
        result = update(self.root, "simulation", "raise the arm", expect_worker_url="http://127.0.0.1:8765")

        self.assertEqual(order, ["freeze", "build", "submit"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "work/model/microban/change")
        self.assertEqual(result["pull_request"], "https://example/pr/1")
        self.assertFalse((self.root / ".git/description-update.lock").exists())

    def test_installed_tool_that_differs_from_the_lock_blocks_before_freezing(self):
        def mismatch(root):
            raise PipelineError("Toolchain differs from lock (package_digest); explicit tool upgrade required")

        order = self._prepare(toolchain=mismatch)
        with self.assertRaises(PipelineError) as raised:
            update(self.root, "simulation", "message")

        self.assertIn("Toolchain differs", str(raised.exception))
        self.assertEqual(order, [])
        self.assertFalse((self.root / ".git/description-update.lock").exists())

    def test_missing_commit_identity_blocks_before_capture(self):
        order = self._prepare()
        original = git_stub()

        def missing_author(root: Path, *args: str, check: bool = True):
            if args[:2] == ("var", "GIT_AUTHOR_IDENT"):
                return subprocess.CompletedProcess(args, 128, "", "Author identity unknown")
            return original(root, *args, check=check)

        with (
            patch("description_pipeline.repository.git", side_effect=missing_author),
            self.assertRaises(PipelineError) as raised,
        ):
            update(self.root, "simulation", "message")
        self.assertIn("configure user.name and user.email", str(raised.exception))
        self.assertEqual(order, [])

    def test_unrelated_workspace_changes_are_refused_before_freezing(self):
        order = self._prepare(status=" M notes.txt\n M config/robot.yaml\n")
        with self.assertRaises(PipelineError) as raised:
            update(self.root, "simulation", "message")

        self.assertIn("dedicated model workspace", str(raised.exception))
        self.assertIn("unexpected=['notes.txt']", str(raised.exception))
        self.assertEqual(order, [])

    def test_non_model_branch_is_refused(self):
        order = self._prepare(branch="main")
        with self.assertRaises(PipelineError):
            update(self.root, "simulation", "message")
        self.assertEqual(order, [])

    def test_stale_tunnel_port_is_refused(self):
        order = self._prepare(worker="http://127.0.0.1:9999")
        with self.assertRaises(PipelineError) as raised:
            update(self.root, "simulation", "message", expect_worker_url="http://127.0.0.1:8765")
        self.assertIn("worker_url", str(raised.exception))
        self.assertEqual(order, [])

    def test_missing_remote_feature_branch_is_refused(self):
        order = self._prepare(feature_exists=False)
        with self.assertRaises(PipelineError) as raised:
            update(self.root, "simulation", "message")
        self.assertIn("feature/microban is not initialised", str(raised.exception))
        self.assertEqual(order, [])

    def test_profile_must_be_valid_before_collecting(self):
        def broken(root, profile):
            raise PipelineError("profile purpose must be simulation")

        order = self._prepare(profile_for=broken)
        with self.assertRaises(PipelineError) as raised:
            update(self.root, "simulation", "message")
        self.assertIn("purpose", str(raised.exception))
        self.assertEqual(order, [])

    def test_hardware_id_must_match_the_branch(self):
        order = self._prepare()
        with (
            patch(
                "description_pipeline.repository.definition",
                return_value={"hardware_id": "other", "source": {"worker_url": "http://127.0.0.1:8765"}},
            ),
            self.assertRaises(PipelineError) as raised,
        ):
            update(self.root, "simulation", "message")
        self.assertIn("hardware_id does not match", str(raised.exception))
        self.assertEqual(order, [])

    def test_concurrent_update_is_refused_by_the_lock(self):
        (self.root / ".git/description-update.lock").write_text("123\n", encoding="utf-8")
        self._prepare()
        with self.assertRaises(PipelineError) as raised:
            update(self.root, "simulation", "message")
        self.assertIn("Another model update holds", str(raised.exception))

    def test_public_submit_requires_the_same_workspace_lock(self):
        self._prepare()
        (self.root / ".git/description-update.lock").write_text("123\n", encoding="utf-8")
        with self.assertRaises(PipelineError) as raised:
            submit(self.root, "simulation", "message")
        self.assertIn("Another model update holds", str(raised.exception))

    def test_missing_or_unauthenticated_github_cli_blocks_before_freezing(self):
        order = self._prepare()
        with (
            patch(
                "description_pipeline.repository._verify_github_cli",
                side_effect=PipelineError("GitHub CLI (gh) is required to submit a candidate"),
            ),
            self.assertRaises(PipelineError) as raised,
        ):
            update(self.root, "simulation", "message")
        self.assertIn("gh", str(raised.exception))
        self.assertEqual(order, [])

        unavailable = subprocess.CalledProcessError(1, ["gh", "--version"], stderr="no such binary")
        with (
            patch("description_pipeline.repository.subprocess.run", side_effect=FileNotFoundError("gh")),
            self.assertRaises(PipelineError) as raised,
        ):
            _verify_github_cli()
        self.assertIn("required", str(raised.exception))
        with (
            patch("description_pipeline.repository.subprocess.run", side_effect=unavailable),
            self.assertRaises(PipelineError) as raised,
        ):
            _verify_github_cli()
        self.assertIn("no such binary", str(raised.exception))

    def test_update_reports_the_review_branch_that_was_pushed(self):
        order = self._prepare()

        def submit_stub(root, profile, message, *, ci=False):
            order.append("submit")
            return {
                "passed": True,
                "state": "pull_request_open",
                "branch": "work/model/microban/20260921T000000Z-ab12cd",
                "model_sha": "e" * 40,
                "pull_request": "https://example/pr/1",
            }

        with patch("description_pipeline.repository._submit", side_effect=submit_stub):
            result = update(self.root, "simulation", "message")
        self.assertEqual(result["branch"], "work/model/microban/20260921T000000Z-ab12cd")
        self.assertEqual(result["model_sha"], "e" * 40)
        self.assertTrue(result["ok"])

    def test_unqualified_build_keeps_the_diagnostic_the_report_names(self):
        order = self._prepare()
        reported = self.root / "build/failed/reported-candidate"
        reported.mkdir(parents=True)
        (reported / "failure.json").write_text("{}\n", encoding="utf-8")
        # An unrelated older failure must never be preferred over the report's own path.
        stale = self.root / "build/failed/zzzz-older"
        stale.mkdir()
        (stale / "failure.json").write_text("{}\n", encoding="utf-8")

        with (
            patch(
                "description_pipeline.repository.build",
                return_value={
                    **REPORT,
                    "passed": False,
                    "blockers": ["consumer.contacts"],
                    "diagnostic_path": str(reported),
                },
            ),
            self.assertRaises(PipelineError) as raised,
        ):
            update(self.root, "simulation", "message")
        self.assertIn("does not qualify", str(raised.exception))
        self.assertEqual(raised.exception.diagnostic_path, str(reported))
        self.assertEqual(order, ["freeze"])

    def test_unqualified_build_never_reaches_submit(self):
        order = self._prepare()
        with (
            patch(
                "description_pipeline.repository.build",
                side_effect=lambda root, profile: {**REPORT, "passed": False, "blockers": ["consumer.contacts"]},
            ),
            self.assertRaises(PipelineError) as raised,
        ):
            update(self.root, "simulation", "message")
        self.assertIn("does not qualify", str(raised.exception))
        self.assertEqual(order, ["freeze"])
        self.assertFalse((self.root / ".git/description-update.lock").exists())


class WorktreeLockTests(unittest.TestCase):
    """The lock must follow the checkout's real git directory, not a literal .git."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()

        def git(*args, cwd=None):
            return subprocess.run(
                ["git", "-C", str(cwd or self.repo), *args], check=True, capture_output=True, text=True
            )

        git("init", "--quiet")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "test")
        (self.repo / "README.md").write_text("model\n", encoding="utf-8")
        git("add", "README.md")
        git("commit", "-q", "-m", "seed")
        git("checkout", "-q", "-b", "feature/microban")
        self.worktree = self.base / "wt"
        git("worktree", "add", "-q", "-b", "work/model/microban/change", str(self.worktree))
        git_path = git("rev-parse", "--git-path", "description-update.lock", cwd=self.worktree).stdout.strip()
        self.lock = Path(git_path)
        if not self.lock.is_absolute():
            self.lock = (self.worktree / self.lock).resolve()

    def test_lock_lives_in_the_worktree_gitdir(self):
        from description_pipeline.repository import _update_lock

        self.assertFalse((self.worktree / ".git").is_dir(), "a linked worktree has a .git file, not a directory")
        path, handle = _update_lock(self.worktree)
        try:
            self.assertEqual(path, self.lock)
            self.assertTrue(path.is_file())
        finally:
            os.close(handle)
            path.unlink()


class SubmitReviewContractTests(unittest.TestCase):
    """The pull request exists before the central run is triggered.

    The GitHub double honours the ``head=`` filter the way the API does, so a re-run on
    the same review branch updates one thread and a candidate on a new branch would show
    up as a second pull request instead of being silently patched.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.calls: list[tuple] = []
        self.pulls: list[dict] = []

    def _github(self):
        def api(endpoint, data=None, *, method=None):
            self.calls.append((endpoint, data, method))
            if "pulls?state=open" in endpoint:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(endpoint).query)
                owner, _, head = query.get("head", [""])[0].partition(":")
                self.assertEqual(owner, "org")
                return [pr for pr in self.pulls if pr["head"]["ref"] == head]
            if endpoint.endswith("/pulls") and data is not None:
                created = {
                    "html_url": f"https://example/pr/{len(self.pulls) + 1}",
                    "number": len(self.pulls) + 1,
                    "base": {"ref": data["base"]},
                    "head": {"ref": data["head"]},
                }
                self.pulls.append(created)
                return created
            if "/pulls/" in endpoint and data is not None:
                number = int(endpoint.rsplit("/", 1)[-1])
                return next(pr for pr in self.pulls if pr["number"] == number)
            return None

        return api

    def _submit(
        self,
        *,
        dispatch_error=None,
        github_error=None,
        branch="work/model/microban/abc",
        ci=False,
        base_contained=True,
        feature_exists=True,
        fetch_fails=False,
        merge_base_error=False,
    ):
        patches = [
            patch("description_pipeline.repository.check_layout", return_value={"role": "model", "passed": True}),
            patch("description_pipeline.repository.assess", return_value=dict(REPORT)),
            patch(
                "description_pipeline.repository.git",
                side_effect=git_stub(
                    branch=branch,
                    base_contained=base_contained,
                    feature_exists=feature_exists,
                    fetch_fails=fetch_fails,
                    merge_base_error=merge_base_error,
                ),
            ),
            patch("description_pipeline.repository.repository_slug", return_value="org/repo"),
            patch(
                "description_pipeline.repository.github_api",
                side_effect=github_error or self._github(),
            ),
        ]
        if dispatch_error is not None:
            patches.append(patch("description_pipeline.repository.dispatch", side_effect=dispatch_error))
        else:
            patches.append(
                patch(
                    "description_pipeline.repository.dispatch",
                    return_value={"state": "dispatched", "workflow": "https://example/workflow"},
                )
            )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return submit(self.root, "simulation", "raise the arm", ci=ci)

    def test_default_submission_succeeds_without_contacting_actions(self):
        result = self._submit(dispatch_error=AssertionError("Actions must not be contacted"))

        self.assertTrue(result["passed"])
        self.assertEqual(result["state"], "pull_request_open")
        self.assertEqual(result["central_validation"], {"state": "not_requested"})
        self.assertEqual(result["pull_request"], "https://example/pr/1")
        self.assertNotIn("advisories", result)
        body = next(data["body"] for endpoint, data, _ in self.calls if endpoint.endswith("/pulls") and data)
        self.assertIn("Local independent verification passed", body)
        self.assertNotIn("pending", body)

    def test_a_review_branch_without_the_current_base_is_flagged(self):
        """The pull request is still valid; the operator is told what the reviewer would merge."""

        result = self._submit(base_contained=False)

        self.assertTrue(result["passed"])
        self.assertEqual(result["state"], "pull_request_open")
        note = result["advisories"][0]
        self.assertEqual(note["code"], "review_branch_behind_base")
        self.assertEqual(note["base"], "feature/microban")
        self.assertIn("does not contain the current feature/microban", note["message"])
        self.assertIn("git merge origin/feature/microban", note["commands"])
        self.assertIn("description model submit", note["then"])

    def test_a_missing_remote_base_is_not_reported_as_moved(self):
        """The control: a hardware branch absent from the remote is not 'behind' it."""

        result = self._submit(base_contained=False, feature_exists=False)

        self.assertTrue(result["passed"])
        self.assertNotIn("advisories", result)

    def test_a_failed_fetch_is_not_reported_as_moved(self):
        """A stale remote-tracking ref describes a branch this machine has not seen."""

        result = self._submit(base_contained=False, fetch_fails=True)

        self.assertTrue(result["passed"])
        self.assertNotIn("advisories", result)

    def test_a_merge_base_error_is_not_reported_as_moved(self):
        """Only git's answer 1 means 'not an ancestor'; anything else is an error."""

        result = self._submit(base_contained=False, merge_base_error=True)

        self.assertTrue(result["passed"])
        self.assertNotIn("advisories", result)

    def test_the_recovery_line_survives_every_shell(self):
        """No path is embedded: double quotes expand ``$`` on POSIX, single quotes break in cmd."""

        from description_pipeline.repository import _base_moved_advisory

        with patch("description_pipeline.repository.git", side_effect=git_stub(base_contained=False)):
            note = _base_moved_advisory(Path("/srv/my models/$robot"), "microban")

        assert note is not None
        self.assertIn("description model submit", note["then"])
        self.assertIn("description model update", note["then"])
        self.assertIn("rebuilds", note["then"])
        self.assertNotIn("$robot", note["then"])
        self.assertNotIn("--root", note["then"])

    def test_pull_request_is_created_before_the_dispatch(self):
        result = self._submit(ci=True)

        self.assertEqual(result["state"], "dispatched")
        self.assertEqual(result["central_validation"]["state"], "dispatched")
        self.assertEqual(result["pull_request"], "https://example/pr/1")

    def test_dispatch_failure_keeps_the_pull_request_and_reports_pending(self):
        result = self._submit(
            dispatch_error=subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 503: no server"), ci=True
        )

        self.assertEqual(result["state"], "pull_request_open")
        self.assertFalse(result["passed"])
        self.assertEqual(result["central_validation"]["state"], "dispatch_failed")
        self.assertIn("model dispatch", result["central_validation"]["retry"])
        self.assertIn("HTTP 503", result["central_validation"]["error"])
        self.assertEqual(result["branch"], "work/model/microban/abc")
        self.assertEqual(result["model_sha"], "c" * 40)
        self.assertEqual(result["pull_request"], "https://example/pr/1")
        self.assertTrue(any(endpoint.endswith("/pulls") and data for endpoint, data, _ in self.calls))

    def test_pr_api_failure_keeps_the_pushed_branch_and_names_the_retry(self):
        failure = subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 403: Resource not accessible")
        result = self._submit(github_error=failure)

        self.assertEqual(result["state"], "pushed_pending_review")
        self.assertFalse(result["passed"])
        self.assertEqual(result["review"]["state"], "not_created")
        self.assertIn("HTTP 403", result["review"]["error"])
        self.assertEqual(result["branch"], "work/model/microban/abc")
        self.assertEqual(result["model_sha"], "c" * 40)
        self.assertIn("model submit", result["retry"])
        self.assertIn(result["branch"], result["retry"])
        self.assertNotIn("pull_request", result)

    def test_rerun_on_the_same_review_branch_updates_the_same_pull_request(self):
        self._submit()
        result = self._submit()

        patched = [
            (endpoint, method) for endpoint, data, method in self.calls if "/pulls/1" in endpoint and data is not None
        ]
        created = [endpoint for endpoint, data, _ in self.calls if endpoint.endswith("/pulls") and data is not None]
        self.assertTrue(patched, "an existing review thread must be updated in place")
        self.assertEqual(patched[0][1], "PATCH", "gh api defaults to POST; the update must be a PATCH")
        self.assertEqual(len(created), 1, "a re-run on the same branch must not open a second pull request")
        self.assertEqual(len(self.pulls), 1)
        self.assertEqual(result["pull_request"], "https://example/pr/1")

    def test_pull_request_retry_preserves_explicit_ci_choice(self):
        failure = subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 503")
        result = self._submit(github_error=failure, ci=True)
        self.assertFalse(result["passed"])
        self.assertIn("--ci", result["retry"])


class ReviewBranchTests(unittest.TestCase):
    """Real git: submitting from the development branch must not move it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.workspace = self.base / "workspace"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        self._git(self.workspace, "remote", "add", "origin", str(self.remote))
        self._git(self.workspace, "config", "user.email", "t@example.invalid")
        self._git(self.workspace, "config", "user.name", "test")
        (self.workspace / "model").mkdir()
        (self.workspace / "model/robot.json").write_text('{"revision": 1}\n', encoding="utf-8")
        self._git(self.workspace, "add", "-A")
        self._git(self.workspace, "commit", "-qm", "seed")
        self._git(self.workspace, "branch", "-M", "feature/microban")
        self._git(self.workspace, "push", "-q", "-u", "origin", "feature/microban")
        self.pulls: list[dict] = []
        self.calls: list[tuple] = []

    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def _github(self):
        def api(endpoint, data=None, *, method=None):
            self.calls.append((endpoint, data, method))
            if "pulls?state=open" in endpoint:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(endpoint).query)
                owner, _, head = query.get("head", [""])[0].partition(":")
                self.assertEqual(owner, "org")
                return [pr for pr in self.pulls if pr["head"]["ref"] == head]
            if endpoint.endswith("/pulls") and data is not None:
                created = {
                    "html_url": f"https://example/pr/{len(self.pulls) + 1}",
                    "number": len(self.pulls) + 1,
                    "base": {"ref": data["base"]},
                    "head": {"ref": data["head"]},
                }
                self.pulls.append(created)
                return created
            if "/pulls/" in endpoint and data is not None:
                number = int(endpoint.rsplit("/", 1)[-1])
                return next(pr for pr in self.pulls if pr["number"] == number)
            return None

        return api

    def _submit(self):
        with (
            patch("description_pipeline.repository.check_layout", return_value={"role": "model", "passed": True}),
            patch("description_pipeline.repository.assess", return_value=dict(REPORT)),
            patch("description_pipeline.repository.repository_slug", return_value="org/repo"),
            patch("description_pipeline.repository.github_api", side_effect=self._github()),
            patch(
                "description_pipeline.repository.dispatch",
                return_value={"state": "dispatched", "workflow": "https://example/workflow"},
            ),
        ):
            return submit(self.workspace, "simulation", "raise the arm")

    def test_development_branch_is_not_advanced_by_a_submission(self):
        (self.workspace / "model/robot.json").write_text('{"revision": 2}\n', encoding="utf-8")
        result = self._submit()

        self.assertRegex(result["branch"], r"^work/model/microban/\d{8}T\d{6}Z-[0-9a-f]{6}$")
        self.assertEqual(self._git(self.workspace, "branch", "--show-current"), result["branch"])
        self.assertEqual(
            self._git(self.workspace, "rev-parse", "feature/microban"),
            self._git(self.workspace, "rev-parse", "origin/feature/microban"),
        )
        self.assertEqual(self._git(self.workspace, "log", "-1", "--format=%s", "feature/microban"), "seed")
        self.assertEqual(self._git(self.workspace, "log", "-1", "--format=%s"), "raise the arm")
        remote = {
            ref: sha
            for sha, ref in (
                line.split() for line in self._git(self.workspace, "ls-remote", "origin", "refs/heads/*").splitlines()
            )
        }
        self.assertIn("refs/heads/" + result["branch"], remote)
        self.assertEqual(
            remote["refs/heads/feature/microban"], self._git(self.workspace, "rev-parse", "feature/microban")
        )

    def test_rerun_on_the_review_branch_updates_the_same_pull_request(self):
        self._submit()
        branch = self._git(self.workspace, "branch", "--show-current")
        (self.workspace / "model/robot.json").write_text('{"revision": 3}\n', encoding="utf-8")
        result = self._submit()

        self.assertEqual(result["branch"], branch)
        self.assertEqual(len(self.pulls), 1)
        self.assertEqual(result["pull_request"], "https://example/pr/1")
        created = [endpoint for endpoint, data, _ in self.calls if endpoint.endswith("/pulls") and data is not None]
        patched = [(endpoint, method) for endpoint, data, method in self.calls if "/pulls/1" in endpoint and data]
        self.assertEqual(len(created), 1)
        self.assertEqual(patched[-1][1], "PATCH")


class SubmitExitCodeTests(unittest.TestCase):
    """An incomplete dispatch is not success, and the payload still names the PR and retry."""

    def test_incomplete_dispatch_exits_non_zero_and_keeps_the_review_thread(self):
        payload = {
            "ok": False,
            "state": "pull_request_open",
            "branch": "work/model/microban/20260921T000000Z-ab12cd",
            "model_sha": "d" * 40,
            "pull_request": "https://example/pr/7",
            "central_validation": {
                "state": "dispatch_failed",
                "error": "HTTP 503: no server",
                "retry": f"description model dispatch --root /model --candidate {'d' * 40} --profile simulation",
            },
        }
        stream = io.StringIO()
        with (
            patch("description_pipeline.cli.update", return_value=payload),
            contextlib.redirect_stdout(stream),
        ):
            status = cli.main(["model", "update", "--root", "/model", "--profile", "simulation", "--message", "m"])

        self.assertEqual(status, 1)
        printed = json.loads(stream.getvalue())
        self.assertEqual(printed["pull_request"], "https://example/pr/7")
        self.assertIn("model dispatch", printed["central_validation"]["retry"])


if __name__ == "__main__":
    unittest.main()
