from __future__ import annotations

import copy
import contextlib
import io
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError
from description_pipeline.cli import main
from description_pipeline.build import tool_identity
from description_pipeline.verification.urdf_quality import ledger as joint_ledger
from description_pipeline.repository import (
    check_layout,
    dispatch,
    git,
    init_model,
    submit,
    update,
    promote,
    promotion_plan,
    validate_commit,
    pending_candidates,
    push,
    _review,
    _error_text,
    _lfs_lock_disable,
    _lfs_lock_verify_failure,
)


class RepositoryTests(unittest.TestCase):
    def test_a_lock_endpoint_error_retries_the_same_push_without_the_lock_query(self):
        """The Windows promotion push died on git-lfs's lock query; nothing else may change."""

        calls: list[tuple] = []
        refspec = "def:refs/heads/release/testbot"
        observed = (
            'Remote "origin" does not support the Git LFS locking API. Consider disabling it with:\n'
            "  $ git config lfs.https://github.com/mimicverse/description.git/info/lfs.locksverify false\n"
            'Post "https://github.com/mimicverse/description.git/info/lfs/locks/verify": EOF\n'
            "error: failed to push some refs to 'https://github.com/mimicverse/description.git'\n"
        )

        def failing_once(root, *arguments, check=True):
            calls.append(arguments)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(128, ["git", "push"], stderr=observed)
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch("description_pipeline.repository.git", side_effect=failing_once),
            patch(
                "description_pipeline.repository._lfs_lock_disable",
                return_value=["-c", "lfs.locksverify=false"],
            ),
        ):
            notes = push(Path("/models/robot"), "--atomic", "origin", refspec)
        self.assertEqual(calls[0], ("push", "--atomic", "origin", refspec))
        self.assertEqual(calls[1], ("-c", "lfs.locksverify=false", "push", "--atomic", "origin", refspec))
        self.assertEqual([note["code"] for note in notes], ["lfs_lock_verify_unavailable"])
        self.assertIn("locks/verify returned EOF", notes[0]["message"])
        self.assertIn("this push only", notes[0]["message"])
        self.assertNotIn("commands", notes[0])

    def test_the_observed_refusal_is_read_from_the_whole_stderr_not_the_short_diagnostic(self):
        """`_error_text` cuts at 300 characters, and the observed message carries the error after that."""

        observed = (
            'Remote "origin" does not support the Git LFS locking API. Consider disabling it with:\n'
            "  $ git config lfs.https://github.com/mimicverse/description.git/info/lfs.locksverify false\n"
            "  $ git config lfs.https://github.com/mimicverse/description.git/info/lfs.locksverify true\n"
            'Post "https://github.com/mimicverse/description.git/info/lfs/locks/verify": EOF\n'
        )
        error = subprocess.CalledProcessError(128, ["git", "push"], stderr=observed)
        self.assertGreater(len(observed), 300, "the fixture has to outgrow the short diagnostic")
        self.assertNotIn("eof", _error_text(error).lower(), "the short diagnostic loses the error itself")
        self.assertEqual(_lfs_lock_verify_failure(error), "returned EOF")
        self.assertEqual(
            _lfs_lock_verify_failure(subprocess.CalledProcessError(1, ["git"], stderr="git lfs locks/verify: EOF")),
            "returned EOF",
        )
        self.assertEqual(
            _lfs_lock_verify_failure(
                subprocess.CalledProcessError(
                    1, ["git"], stderr='Post "https://x/locks/verify": 503 Service Unavailable'
                )
            ),
            "answered 503",
        )

    def test_a_lock_conflict_or_another_push_failure_is_not_retried(self):
        """Only an unreachable lock endpoint is retried; a real lock holder still stops the push."""

        for stderr in (
            "remote: Git LFS: cannot update locked file meshes/base.stl (locked by alice)\n",
            "fatal: Authentication failed for 'https://github.com/owner/repo.git/'\n",
        ):
            with (
                self.subTest(stderr=stderr),
                patch(
                    "description_pipeline.repository.git",
                    side_effect=subprocess.CalledProcessError(128, ["git", "push"], stderr=stderr),
                ) as command,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                push(Path("/models/robot"), "origin", "main")
            self.assertEqual(command.call_count, 1)

    def test_the_url_scoped_override_strips_the_endpoint_auth_annotation(self):
        """`git lfs env` prints `Endpoint=<url> (auth=basic)`; the config key is the URL alone."""

        environment = (
            "git-lfs/3.7.1 (GitHub; linux amd64; go 1.25.3; git b84b3384)\n"
            "Endpoint=https://github.com/mimicverse/description.git/info/lfs (auth=basic)\n"
            "LocalWorkingDir=/models/robot\n"
        )
        with patch(
            "description_pipeline.repository.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, environment, ""),
        ):
            overrides = _lfs_lock_disable(Path("/models/robot"))
        self.assertEqual(
            overrides,
            [
                "-c",
                "lfs.locksverify=false",
                "-c",
                "lfs.https://github.com/mimicverse/description.git/info/lfs.locksverify=false",
            ],
        )

    def test_a_push_that_works_returns_no_advisory(self):
        with patch("description_pipeline.repository.git", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(push(Path("/models/robot"), "origin", "main"), [])

    def test_model_init_scaffolds_the_joint_ledger_the_pipeline_requires(self):
        """A new workspace starts with the empty ledger, so filling it in is the visible next step."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "model"
            init_model(root, "fixture", {"provider": "fixture", "path": str(Path(temporary) / "source")})
            text = (root / "config/joint_names.yaml").read_text(encoding="utf-8")
            self.assertIn("structural_joint_names: []", text)
            self.assertIn("URDF208", text)
            self.assertEqual(joint_ledger.load(root / "config/joint_names.yaml"), [])

    def test_every_model_operation_refuses_a_directory_that_is_not_a_checkout(self):
        """`diff` had the sentence; the `model` operations answered with a raw CalledProcessError."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sha = "a" * 40
            calls = (
                ("update", lambda: update(root, "kinematics")),
                ("submit", lambda: submit(root, "kinematics", "message")),
                ("validate", lambda: validate_commit(root, sha, "kinematics")),
                ("promote", lambda: promotion_plan(root, "testbot", sha, "kinematics")),
                ("dispatch", lambda: dispatch(root, sha, "kinematics")),
                ("pending", lambda: pending_candidates(root, "kinematics")),
            )
            for name, call in calls:
                with self.subTest(entry=name), self.assertRaises(PipelineError) as caught:
                    call()
                self.assertIn("Not a Git checkout", str(caught.exception))
                self.assertIn("description model init", str(caught.exception))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "tool.py").write_text("tooling\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "tooling base")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "origin", "main")

    def test_model_worktree_keeps_history_but_contains_only_assets(self):
        root = self.base / "model"
        original = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        init_model(root, "testbot", {"provider": "fixture", "path": "source"}, repository=self.repo)
        self.assertTrue(check_layout(root)["passed"])
        self.assertFalse((root / "tool.py").exists())
        self.assertEqual(git(root, "branch", "--show-current").stdout.strip(), "feature/testbot")
        self.assertEqual(git(root, "rev-parse", "HEAD").stdout.strip(), original)
        self.assertTrue((self.repo / "tool.py").exists())

    def test_init_rejects_a_base_that_git_would_read_as_an_option(self):
        root = self.base / "option-model"
        with self.assertRaisesRegex(PipelineError, "Invalid base revision"):
            init_model(
                root,
                "testbot",
                {"provider": "fixture", "path": "source"},
                repository=self.repo,
                base="--upload-pack=evil",
            )
        self.assertFalse(root.exists(), "a rejected base must not create a worktree")

    def test_init_accepts_a_branch_as_the_base(self):
        root = self.base / "branch-model"
        init_model(root, "branchbot", {"provider": "fixture", "path": "source"}, repository=self.repo, base="main")
        self.assertTrue((root / "config/robot.yaml").is_file())

    def test_tool_identity_binds_deployment_scripts_and_dependency_locks(self):
        package = self.base / "isolated/src/description_pipeline"
        package.mkdir(parents=True)
        script = package / "worker.ps1"
        lock = package / "requirements.lock"
        script.write_text("first deployment")
        lock.write_text("first lock")
        with patch("description_pipeline.build.__file__", str(package / "build/__init__.py")):
            first = tool_identity()["package_digest"]
            script.write_text("changed deployment")
            second = tool_identity()["package_digest"]
            lock.write_text("changed lock")
            third = tool_identity()["package_digest"]
        self.assertEqual(len({first, second, third}), 3)

    def test_model_checkout_preserves_evidence_bytes_with_either_line_ending_setting(self):
        root = self.base / "model"
        init_model(root, "testbot", {"provider": "fixture", "path": "source"}, repository=self.repo)
        evidence = {
            "docs/provenance/vendor.html": b"<html>Original evidence\r\n</html>\r\n",
            "mjcf/robot.xml": b"<mujoco>\n</mujoco>\n",
        }
        for name, data in evidence.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        git(root, "config", "core.autocrlf", "true")
        git(root, "add", ".")
        git(root, "commit", "-m", "Preserve frozen evidence")
        for name, data in evidence.items():
            committed = subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=root)
            self.assertEqual(committed, data)
        for setting in ("true", "false"):
            with self.subTest(autocrlf=setting):
                checkout = self.base / f"consumer-{setting}"
                git(self.base, "clone", "--no-checkout", str(root), str(checkout))
                git(checkout, "config", "core.autocrlf", setting)
                git(checkout, "checkout", "--force", "HEAD")
                for name, data in evidence.items():
                    self.assertEqual((checkout / name).read_bytes(), data)

    def test_submit_existing_feature_creates_candidate_pr_without_pushing_feature(self):
        git(self.repo, "switch", "-c", "feature/testbot")
        git(self.repo, "push", "origin", "feature/testbot")
        previous = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "tool.py").write_text("new model candidate\n")
        requests = []

        def api(endpoint, data=None):
            requests.append((endpoint, data))
            return [] if data is None else {"html_url": "https://github.com/owner/repo/pull/2", "number": 2}

        with (
            patch("description_pipeline.repository.check_layout"),
            patch(
                "description_pipeline.repository.assess",
                return_value={
                    "passed": True,
                    "hardware_id": "testbot",
                    "subject": "c" * 64,
                },
            ),
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.dispatch", return_value={"state": "dispatched"}) as dispatched,
            patch("description_pipeline.repository.github_api", side_effect=api),
        ):
            response = submit(self.repo, "kinematics", "Review candidate")
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        branch = git(self.repo, "branch", "--show-current").stdout.strip()
        self.assertRegex(branch, r"^work/model/testbot/\d{8}T\d{6}Z-[0-9a-f]{6}$")
        self.assertEqual(git(self.repo, "branch", "--show-current").stdout.strip(), branch)
        # The development branch keeps its commit: the candidate only lives on the review branch.
        self.assertEqual(git(self.repo, "rev-parse", "feature/testbot").stdout.strip(), previous)
        self.assertEqual(
            git(self.repo, "ls-remote", "origin", "refs/heads/feature/testbot").stdout.split()[0], previous
        )
        self.assertEqual(git(self.repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()[0], sha)
        dispatched.assert_not_called()
        self.assertTrue(response["passed"])
        self.assertEqual(requests[-1][1]["base"], "feature/testbot")
        self.assertEqual(requests[-1][1]["head"], branch)
        self.assertEqual(response["pull_request"], "https://github.com/owner/repo/pull/2")

    def test_validation_checks_detached_exact_commit_and_cleans_worktree(self):
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        visited = []

        def assess(root, profile):
            visited.append(root)
            self.assertEqual(git(root, "rev-parse", "HEAD").stdout.strip(), sha)
            self.assertEqual(git(root, "branch", "--show-current").stdout.strip(), "")
            return {"passed": True}

        with (
            patch("description_pipeline.repository.check_layout"),
            patch("description_pipeline.repository.assess", side_effect=assess),
        ):
            self.assertEqual(validate_commit(self.repo, sha, "kinematics")["model_sha"], sha)
        self.assertFalse(visited[0].exists())

    def test_remote_validation_downloads_the_committed_bytes_into_a_fresh_store(self):
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "tool.py").write_text("local uncommitted content")

        def check(root, profile):
            self.assertEqual((root / "tool.py").read_text(encoding="utf-8"), "tooling\n")
            self.assertTrue((root / ".git").is_dir())
            return {"passed": True}

        with (
            patch("description_pipeline.repository.check_layout"),
            patch("description_pipeline.repository.assess", side_effect=check),
        ):
            result = validate_commit(self.repo, sha, "kinematics", remote=True)
        self.assertTrue(result["delivery_retrieved_from_remote"])

    def test_cli_remote_validation_uses_a_fresh_checkout(self):
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "tool.py").write_text("uncommitted changes must not qualify")

        def assess_remote(root, profile):
            self.assertEqual((root / "tool.py").read_text(encoding="utf-8"), "tooling\n")
            self.assertNotEqual(root, self.repo)
            return {"passed": True}

        output = io.StringIO()
        with (
            patch("description_pipeline.repository.check_layout"),
            patch("description_pipeline.repository.assess", side_effect=assess_remote),
            contextlib.redirect_stdout(output),
        ):
            result = main(["model", "validate", "--root", str(self.repo), "--candidate", sha, "--remote"])
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())["delivery_retrieved_from_remote"])

    def test_tool_resolution_uses_commit_and_digest_without_requiring_a_tag(self):
        resolve = runpy.run_path(str(Path(__file__).resolve().parents[2] / ".github/scripts/resolve_model_tool.py"))[
            "resolve"
        ]
        tool_sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        path = self.repo / "config/toolchain.lock.json"
        path.parent.mkdir()
        lock = {
            "development": False,
            "source_commit": tool_sha,
            "version": "0.3.0",
            "package_digest": "c" * 64,
            "python": "3.12.14",
            "platform": {"system": "Linux", "machine": "x86_64", "implementation": "CPython"},
        }
        path.write_text(json.dumps(lock))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "model lock")
        candidate = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(resolve(self.repo, self.repo, candidate, "kinematics")["tool_sha"], tool_sha)
        lock["package_digest"] = "unknown"
        path.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "content digest"):
            resolve(self.repo, self.repo, candidate, "kinematics")

    def test_promotion_can_publish_by_commit_without_any_tags(self):
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "push", "origin", f"{sha}:refs/heads/feature/testbot")
        report = {
            "passed": True,
            "hardware_id": "testbot",
            "subject": "c" * 64,
            "source": {"evidence_class": "cad"},
            "toolchain": {"development": False, "source_commit": sha, "version": "0.3.0"},
        }
        with (
            patch("description_pipeline.repository.validate_commit", return_value=report) as validated,
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.github_api") as api,
        ):
            plan = promotion_plan(self.repo, "testbot", sha, "kinematics")
            promote(self.repo, plan)
        self.assertFalse(plan["ci"])
        self.assertEqual(validated.call_count, 2)
        validated.assert_called_with(self.repo, sha, "kinematics", remote=True)
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0], f"repos/owner/repo/statuses/{sha}")
        self.assertEqual(api.call_args.args[1]["target_url"], f"https://github.com/owner/repo/commit/{sha}")
        self.assertEqual(git(self.repo, "ls-remote", "origin", "refs/heads/release/testbot").stdout.split()[0], sha)
        self.assertEqual(git(self.repo, "ls-remote", "origin", "refs/tags/*").stdout.strip(), "")

    def test_promotion_carries_the_lock_check_it_could_not_run(self):
        """A retried lock query is recorded, not hidden: the release record has to say so."""

        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "push", "origin", f"{sha}:refs/heads/feature/testbot")
        report = {
            "passed": True,
            "hardware_id": "testbot",
            "subject": "c" * 64,
            "source": {"evidence_class": "cad"},
            "toolchain": {"development": False, "source_commit": sha, "version": "0.3.0"},
        }
        note = {"code": "lfs_lock_verify_unavailable", "message": "the lock endpoint did not answer"}
        with (
            patch("description_pipeline.repository.validate_commit", return_value=report),
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.github_api"),
            patch("description_pipeline.repository.push", return_value=[note]) as retried,
        ):
            plan = promotion_plan(self.repo, "testbot", sha, "kinematics")
            result = promote(self.repo, plan)
        self.assertEqual(result["state"], "published")
        self.assertEqual(result["advisories"], [note])
        self.assertEqual(retried.call_args.args[1], "--atomic")

    def test_dispatch_status_and_workflow_bind_same_sha(self):
        sha = "a" * 40
        calls = []
        with (
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.github_api", side_effect=lambda *args: calls.append(args)),
        ):
            dispatch(self.repo, sha, "kinematics")
        self.assertTrue(calls[0][0].endswith(sha))
        self.assertEqual(calls[0][1]["state"], "pending")
        self.assertEqual(calls[1][1]["inputs"]["model_sha"], sha)
        self.assertEqual(calls[1][1]["ref"], "main")

    def test_pending_is_read_only_unless_ci_is_requested(self):
        sha = "a" * 40

        def api(endpoint):
            if "/git/matching-refs/" in endpoint:
                return [{"object": {"sha": sha}}]
            if "/pulls?" in endpoint:
                return []
            if endpoint.endswith("/status"):
                return {"statuses": []}
            raise AssertionError(f"Unexpected GitHub request: {endpoint}")

        with (
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.github_api", side_effect=api),
            patch("description_pipeline.repository.dispatch", return_value={"state": "dispatched"}) as dispatched,
        ):
            pending = pending_candidates(self.repo, "kinematics")
            self.assertEqual(pending[0]["model_sha"], sha)
            self.assertEqual(pending[0]["state"], "not_requested")
            dispatched.assert_not_called()
            self.assertEqual(pending_candidates(self.repo, "kinematics", ci=True), [{"state": "dispatched"}])
            dispatched.assert_called_once_with(self.repo, sha, "kinematics")

    def test_stale_promotion_or_unvalidated_review_cannot_push(self):
        plan = {
            "hardware": "testbot",
            "candidate": "a" * 40,
            "profile": "kinematics",
            "tag": "model/testbot/1.0.0",
            "previous_release": "b" * 40,
            "release_ref": "refs/heads/release/testbot",
            "subject": "c" * 64,
        }
        fresh = copy.deepcopy(plan)
        fresh["previous_release"] = "d" * 40
        with (
            patch("description_pipeline.repository.promotion_plan", return_value=fresh),
            patch("description_pipeline.repository.git") as command,
            self.assertRaisesRegex(PipelineError, "Stale"),
        ):
            promote(self.repo, plan)
        command.assert_not_called()

    def test_promote_prints_the_plan_until_apply_is_given(self):
        """The runbook step at the CLI boundary: verifying is the default, publishing is opt-in."""

        plan = {"schema_version": "description.promotion/v1", "hardware": "testbot", "candidate": "d" * 40}
        arguments = [
            "model",
            "promote",
            "--root",
            str(self.repo),
            "--hardware",
            "testbot",
            "--candidate",
            "d" * 40,
            "--profile",
            "kinematics",
        ]
        with (
            patch("description_pipeline.cli.promotion_plan", return_value=dict(plan)) as planned,
            patch("description_pipeline.cli.promote", return_value={**plan, "state": "published"}) as published,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(arguments), 0)
            published.assert_not_called()
        planned.assert_called_once()

        with (
            patch("description_pipeline.cli.promotion_plan", return_value=dict(plan)) as planned,
            patch("description_pipeline.cli.promote", return_value={**plan, "state": "published"}) as published,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main([*arguments, "--apply", "--review-evidence", "https://github.com/mimicverse/description/pull/1"]),
                0,
            )
        published.assert_called_once()
        self.assertEqual(
            published.call_args.args[1]["review_evidence"], "https://github.com/mimicverse/description/pull/1"
        )

    def test_successful_run_for_another_candidate_cannot_qualify_release(self):
        sha = "a" * 40
        plan = {
            "hardware": "testbot",
            "candidate": sha,
            "profile": "simulation",
            "ci": True,
            "review_evidence": "https://github.com/owner/repo/pull/1",
        }
        pull = {
            "merged": True,
            "base": {"ref": "feature/testbot"},
            "head": {"sha": sha},
            "merge_commit_sha": sha,
            "user": {"login": "author"},
        }
        review = {"state": "APPROVED", "commit_id": sha, "user": {"login": "reviewer"}}
        status = {
            "statuses": [
                {
                    "context": "description/simulation",
                    "state": "success",
                    "target_url": "https://github.com/owner/repo/actions/runs/42",
                }
            ]
        }
        run = {
            "conclusion": "success",
            "path": ".github/workflows/model-validation.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "display_title": f"qualify {'b' * 40} (simulation)",
        }
        with (
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository._pages", return_value=[review]),
            patch("description_pipeline.repository.github_api", side_effect=[pull, status, run]),
            self.assertRaisesRegex(PipelineError, "central validation run"),
        ):
            _review(self.repo, plan)

    def test_local_promotion_rejects_failed_revalidation_and_unqualified_inputs(self):
        sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "push", "origin", f"{sha}:refs/heads/feature/testbot")
        report = {
            "passed": True,
            "hardware_id": "testbot",
            "subject": "c" * 64,
            "source": {"evidence_class": "cad"},
            "toolchain": {"development": False, "source_commit": sha},
        }
        with patch("description_pipeline.repository.validate_commit", return_value=report):
            plan = promotion_plan(self.repo, "testbot", sha, "kinematics")
        for invalid in (
            {**report, "passed": False},
            {**report, "hardware_id": "another-robot"},
            {**report, "source": {"evidence_class": "fixture"}},
            {**report, "toolchain": {"development": True, "source_commit": sha}},
        ):
            with (
                self.subTest(report=invalid),
                patch("description_pipeline.repository.validate_commit", return_value=invalid),
                patch("description_pipeline.repository.github_api") as api,
                self.assertRaises(PipelineError),
            ):
                promote(self.repo, plan)
            api.assert_not_called()
            self.assertEqual(git(self.repo, "ls-remote", "origin", "refs/heads/release/testbot").stdout.strip(), "")

    def test_qualified_exact_sha_is_pushed_atomically_and_tag_cannot_be_reused(self):
        base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "push", "origin", f"{base}:refs/heads/release/testbot", f"{base}:refs/tags/tool/v0.2.0")
        (self.repo / "tool.py").write_text("candidate\n")
        git(self.repo, "commit", "-am", "candidate")
        candidate = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "push", "origin", f"{candidate}:refs/heads/feature/testbot")
        report = {
            "passed": True,
            "hardware_id": "testbot",
            "subject": "c" * 64,
            "source": {"evidence_class": "cad"},
            "toolchain": {"development": False, "source_commit": base, "version": "0.2.0"},
        }
        with (
            patch("description_pipeline.repository.validate_commit", return_value=report),
            patch(
                "description_pipeline.repository._review", return_value="https://github.com/owner/repo/actions/runs/1"
            ),
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.github_api"),
        ):
            plan = promotion_plan(self.repo, "testbot", candidate, "kinematics", "model/testbot/1.0.0")
            promote(self.repo, plan)
            with self.assertRaisesRegex(PipelineError, "tag already exists"):
                promotion_plan(self.repo, "testbot", candidate, "kinematics", "model/testbot/1.0.0")
        for reference in ("refs/heads/release/testbot", "refs/tags/model/testbot/1.0.0"):
            self.assertEqual(git(self.repo, "ls-remote", "origin", reference).stdout.split()[0], candidate)
        with (
            patch("description_pipeline.repository.promotion_plan", return_value=plan),
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.git") as command,
            self.assertRaisesRegex(PipelineError, "PR URL"),
        ):
            promote(self.repo, {**plan, "review_evidence": "looks good"})
        command.assert_not_called()
