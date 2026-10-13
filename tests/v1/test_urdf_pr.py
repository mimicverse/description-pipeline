"""Real local bare-Git tests for the fast-forward single-PR publication boundary."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import write_delivery_manifest, subject_digest
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.io import digest
from description_pipeline.repository import urdf_pr
from description_pipeline.schemas import CHECK_POLICY_SCHEMA, SOURCE_RECORD_SCHEMA
from description_pipeline.stages import CONTRACT

BRANCH = "work/solidworks/m3.0"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Fixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="urdf-pr-test-")
        self.tmp = Path(self.temp.name)
        self.remote = self.tmp / "remote.git"
        run("git", "init", "--bare", "-b", "feature/m3.0", str(self.remote))
        self.seed = self.tmp / "seed"
        self.seed.mkdir()
        run("git", "init", "-b", "feature/m3.0", cwd=self.seed)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("core.autocrlf", "false")):
            run("git", "config", key, value, cwd=self.seed)
        (self.seed / "README.md").write_bytes(b"base\n")
        run("git", "add", "-A", cwd=self.seed)
        run("git", "commit", "-m", "base", cwd=self.seed)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.seed)
        run("git", "push", "-u", "origin", "feature/m3.0", cwd=self.seed)
        self.repo = self.tmp / "repo"
        run("git", "clone", "-c", "core.autocrlf=false", str(self.remote), str(self.repo))
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            run("git", "config", key, value, cwd=self.repo)
        self.bundle = self.tmp / "bundle"
        self.write_bundle("one\n")
        self.seal("r1", None, cad="part-1\n")

    def write_manifest(self) -> None:
        write_delivery_manifest(
            self.bundle,
            contract={"schema_version": CONTRACT["schema_version"], "digest": "a" * 64},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )

    def seal(self, revision_id: str | None, parent: str | None, *, cad: str = "part-1\n", fresh: bool = False) -> None:
        """Write the frozen input/source.json content identity for one revision."""

        inputs = self.bundle / "input"
        inputs.mkdir(parents=True, exist_ok=True)
        (inputs / "M3.0.SLDASM").write_text(cad, encoding="utf-8")
        if fresh:
            (inputs / "source.json").unlink(missing_ok=True)
        identity = {
            "kind": "source-revision",
            "handoff_digest": digest(cad),
            "main_assembly": "M3.0.SLDASM",
            "configuration": "Default",
            "hardware_id": "m3.0",
            "records": [],
        }
        source = {
            "schema_version": SOURCE_RECORD_SCHEMA,
            "source_revision_id": digest(identity),
            "identity": identity,
            "selection": {
                "main_assembly": "M3.0.SLDASM",
                "configuration": "Default",
                "hardware_id": "m3.0",
                "hardware_source": "cad",
            },
            "metadata": {
                "revision": revision_id,
                "owner": "t",
                "control": {"system": "handoff", "reference": "local-handoff"},
                "change_summary": "t",
                "change_summary_source": "submission",
            },
        }
        (inputs / "source.json").write_text(
            json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.write_manifest()

    def write_bundle(self, evidence: str, extra: dict[str, str] | None = None) -> None:
        files = {
            "input/handoff.json": "{}\n",
            "evidence/discovery.json": evidence,
            "model/robot.json": "{}\n",
            "urdf/robot.urdf": "<robot name='m3.0'/>\n",
            "meshes/part.stl": "solid\n",
            "tool.json": "{}\n",
            "reports/quality.json": "{}\n",
        }
        files.update(extra or {})
        for rel, text in files.items():
            path = self.bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if (self.bundle / "input/source.json").is_file():
            self.write_manifest()

    def remote_head(self, branch: str = BRANCH) -> str:
        out = run("git", "ls-remote", str(self.remote), f"refs/heads/{branch}").strip()
        return out.split()[0] if out else ""

    def remote_tree(self, branch: str = BRANCH) -> list[str]:
        run("git", "fetch", "--quiet", "origin", branch, cwd=self.repo)
        return run("git", "ls-tree", "-r", "--name-only", "FETCH_HEAD", cwd=self.repo).splitlines()

    def is_ancestor(self, older: str, newer: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", older, newer], capture_output=True
            ).returncode
            == 0
        )


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.temp.cleanup)
        self.gh_calls: list[tuple[str, ...]] = []
        self.rest_payloads: list[dict] = []
        self.gh_mode = "ok"
        self.pr_exists = False

        def fake_gh(repository: Path, *args: str) -> str:
            self.gh_calls.append(args)
            if self.gh_mode == "fail":
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh down")
            if args[:2] == ("pr", "list"):
                if self.pr_exists:
                    return json.dumps(
                        [
                            {
                                "url": "https://github.com/example/m3.0/pull/1",
                                "number": 1,
                                "baseRefName": "feature/m3.0",
                                "headRefName": BRANCH,
                            }
                        ]
                    )
                return "[]"
            if args[:2] == ("pr", "create"):
                self.pr_exists = True
                return "https://github.com/example/m3.0/pull/1\n"
            if args[0] == "api":
                self.assertEqual(args[:4], ("api", "--method", "PATCH", "repos/example/m3.0/pulls/1"))
                self.rest_payloads.append(json.loads(Path(args[args.index("--input") + 1]).read_text(encoding="utf-8")))
                if self.gh_mode == "fail_update":
                    raise subprocess.CalledProcessError(1, ["gh", *args], stderr="REST update unavailable")
                return "{}"
            if args[:2] == ("pr", "view"):
                if self.gh_mode == "fail_view":
                    raise subprocess.CalledProcessError(1, ["gh", *args], stderr="temporary view failure")
                return json.dumps(
                    {
                        "url": "https://github.com/example/m3.0/pull/1",
                        "state": "OPEN",
                        "baseRefName": "feature/m3.0",
                        "headRefName": BRANCH,
                        "headRefOid": "0" * 40 if self.gh_mode == "stale_head" else self.fx.remote_head(),
                        "isCrossRepository": False,
                    }
                )
            raise AssertionError(args)

        def verifier(bundle: Path) -> dict:
            report = {
                "passed": True,
                "subject_sha256": subject_digest(bundle),
                "checks": [{"id": "x", "status": "passed"}],
            }
            (bundle / "reports/quality.json").write_text(urdf_pr._serialize(report), encoding="utf-8")
            return report

        self.patchers = [
            mock.patch.object(urdf_pr, "_gh", fake_gh),
            mock.patch.object(urdf_pr, "_load_verifier", lambda: verifier),
            mock.patch.object(urdf_pr, "_origin_slug", lambda repository: "example/m3.0"),
            mock.patch.object(urdf_pr.time, "sleep"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

    def test_pr_actual_head_must_equal_verified_pushed_commit(self):
        self.gh_mode = "stale_head"
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "pr_identity_mismatch")
        self.assertEqual(self.fx.remote_head(), result["commit"])
        self.assertTrue(result["detail"]["observed"]["url"])

    def test_failure_after_pr_creation_preserves_url_and_pushed_commit(self):
        self.gh_mode = "fail_view"
        result = self.submit()
        self.assertEqual(result["state"], "gh_failed_after_push")
        self.assertEqual(result["url"], "https://github.com/example/m3.0/pull/1")
        self.assertEqual(result["commit"], self.fx.remote_head())

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
        self.assertGreaterEqual(len(self.rest_payloads), 2)

    def test_extra_reports_receipts_are_refused(self) -> None:
        """Only reports/quality.json may sit outside the subject; old receipts move to run storage."""
        self.fx.write_bundle("one\n", extra={"reports/mass-closure.json": "{}\n"})
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_only_the_quality_report_travels_outside_the_subject(self) -> None:
        self.fx.write_bundle("one\n")
        result = self.submit()
        self.assertIn(result["state"], {"published", "updated"})
        self.assertEqual(
            sorted(path for path in self.fx.remote_tree() if path.startswith("reports/")), ["reports/quality.json"]
        )
        worktree = Path(tempfile.mkdtemp(dir=self.fx.tmp))
        urdf_pr._copy_governed(self.fx.bundle, worktree)
        copied = sorted(
            path.relative_to(worktree).as_posix() for path in (worktree / "reports").rglob("*") if path.is_file()
        )
        self.assertEqual(copied, ["reports/quality.json"])
        self.assertEqual(subject_digest(worktree), subject_digest(self.fx.bundle))

    def test_missing_quality_report_is_refused(self) -> None:
        (self.fx.bundle / "reports/quality.json").unlink()
        result = self.submit()
        self.assertEqual(result["error"], "bundle_incomplete")
        self.assertEqual(self.fx.remote_head(), "")

    def test_original_record_bytes_survive_git_and_fresh_checkout(self):
        name = "input/records/specification.txt"
        payload = b'\xef\xbb\xbf{"drive": {\r\n  "effort": 12,\r\n  "velocity": 2\r\n}}\r\n'
        path = self.fx.bundle / name
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
        self.fx.write_manifest()
        for setting in ("true", "false", "input"):
            with self.subTest(core_autocrlf=setting):
                run("git", "config", "core.autocrlf", setting, cwd=self.fx.repo)
                before = subject_digest(self.fx.bundle)
                result = self.submit()
                self.assertIn(result["state"], {"published", "noop"})
                self.assertEqual(result["subject"], before)
                run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
                committed = subprocess.check_output(["git", "show", f"FETCH_HEAD:{name}"], cwd=self.fx.repo)
                self.assertEqual(committed, payload)
                clone = self.fx.tmp / f"checkout-{setting}"
                run(
                    "git",
                    "clone",
                    "-q",
                    "-c",
                    f"core.autocrlf={setting}",
                    "--branch",
                    BRANCH,
                    str(self.fx.remote),
                    str(clone),
                )
                self.assertEqual((clone / name).read_bytes(), payload)
                self.assertEqual(subject_digest(clone), before)
                self.assertEqual((clone / ".gitattributes").read_bytes(), urdf_pr.GIT_ATTRIBUTES)

    def test_global_filters_and_encoding_do_not_transform_delivery(self):
        attributes = self.fx.tmp / "global-attributes"
        attributes.write_text("input/** text filter=must_not_run working-tree-encoding=UTF-16\n", encoding="ascii")
        run("git", "config", "core.attributesfile", str(attributes), cwd=self.fx.repo)
        run("git", "config", "filter.must_not_run.clean", "description-filter-must-not-run", cwd=self.fx.repo)
        run("git", "config", "filter.must_not_run.required", "true", cwd=self.fx.repo)
        result = self.submit()
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.fx.remote_head(), result["commit"])

    def test_deeper_attributes_block_before_commit_or_push(self):
        self.fx.write_bundle("one\n", extra={"input/.gitattributes": "* text=auto\n"})
        result = self.submit()
        self.assertEqual(result["error"], "git_attributes_conflict")
        self.assertEqual(result["detail"]["attribute"], "text")
        self.assertTrue(result["detail"]["file"].startswith("input/"))
        self.assertEqual(self.fx.remote_head(), "")

    def test_info_attribute_overrides_block_before_commit_or_push(self):
        attributes = self.fx.repo / ".git/info/attributes"
        for rule in ("text=auto", "filter=external", "working-tree-encoding=UTF-16"):
            with self.subTest(attribute=rule):
                attributes.write_text(f"tool.json {rule}\n", encoding="ascii")
                result = self.submit()
                self.assertEqual(result["error"], "git_attributes_conflict")
                self.assertEqual(result["detail"]["file"], "tool.json")
                self.assertEqual(result["detail"]["attribute"], rule.split("=", 1)[0])
                self.assertEqual(self.fx.remote_head(), "")

    def test_bundle_cannot_carry_its_own_git_policy(self):
        self.fx.write_bundle("one\n", extra={".gitattributes": "* text\n"})
        result = self.submit()
        # The generated policy is publisher-owned metadata: the bundle can neither
        # carry nor replace it, and unregistered files are refused outright.
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_native_filenames_with_unicode_and_spaces_publish_unchanged(self):
        name = "input/结构 零件.SLDPRT"
        self.fx.write_bundle("one\n", extra={name: "neutral engineering fixture\n"})
        self.fx.seal("r1", None, fresh=True)
        result = self.submit()
        self.assertEqual(result["state"], "published")
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        actual = subprocess.check_output(["git", "show", f"FETCH_HEAD:{name}"], cwd=self.fx.repo)
        self.assertEqual(actual, (self.fx.bundle / name).read_bytes())

    def test_nonregular_git_policy_does_not_write_outside_worktree(self):
        outside = self.fx.tmp / "outside-policy"
        outside.write_bytes(b"original\n")
        worktree = self.fx.tmp / "policy-symlink"
        worktree.mkdir()
        try:
            (worktree / ".gitattributes").symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation requires platform privileges")
        with self.assertRaises(urdf_pr.PrError) as caught:
            urdf_pr._preserve_git_bytes(worktree)
        self.assertEqual(caught.exception.code, "git_metadata_nonregular")
        self.assertEqual(outside.read_bytes(), b"original\n")

    def test_committed_policy_tampering_still_blocks_push(self):
        hook = self.fx.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf '* text\\n' > .gitattributes\ngit add .gitattributes\n", encoding="ascii")
        hook.chmod(0o755)
        result = self.submit()
        self.assertIn(result["error"], {"committed_git_metadata_mismatch", "commit_left_dirty"})
        self.assertEqual(self.fx.remote_head(), "")

    def test_existing_pr_update_preserves_utf8_metadata(self):
        self.submit()
        message = 'Update left wrist — 左腕 "$literal" `text`\nDetailed review'
        result = self.submit(message=message)
        self.assertEqual(result["state"], "noop")
        self.assertEqual(set(self.rest_payloads[-1]), {"title", "body"})
        self.assertEqual(self.rest_payloads[-1]["title"], message.splitlines()[0])
        self.assertIn(f"verified commit: `{result['commit']}`", self.rest_payloads[-1]["body"])
        self.assertIn(f"subject: `{result['subject']}`", self.rest_payloads[-1]["body"])
        self.assertFalse(any(call[:2] == ("pr", "edit") for call in self.gh_calls))

    def test_failed_existing_pr_update_preserves_url_and_pushed_commit(self):
        first = self.submit()
        self.gh_mode = "fail_update"
        result = self.submit()
        self.assertEqual(result["state"], "gh_failed_after_push")
        self.assertEqual(result["url"], first["url"])
        self.assertEqual(result["commit"], self.fx.remote_head())

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
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: lambda bundle: {"passed": False}):
            self.assertEqual(self.submit()["error"], "verification_failed")
        self.assertEqual(self.fx.remote_head(), "")
        with mock.patch.object(
            urdf_pr, "_load_verifier", lambda: lambda bundle: {"passed": True, "subject_sha256": "0" * 64}
        ):
            self.assertEqual(self.submit()["error"], "verification_subject_mismatch")
        self.assertEqual(self.fx.remote_head(), "")
        good = {"passed": True, "subject_sha256": subject_digest(self.fx.bundle)}
        (self.fx.bundle / "reports/quality.json").write_text('{"passed": true}\n', encoding="utf-8")
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: lambda bundle: good):
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

    def test_unregistered_files_are_refused_not_smuggled(self) -> None:
        self.fx.write_bundle("four\n", extra={"secret.txt": "shh\n"})
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_local_receipts_are_refused_under_reports(self) -> None:
        self.fx.write_bundle("one\n", extra={"reports/run.json": '{"run": 1}\n'})
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

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
        (self.fx.bundle / "Tool.json").write_text("dup\n", encoding="utf-8")
        if (self.fx.bundle / "Tool.json").samefile(self.fx.bundle / "tool.json"):
            self.skipTest("Case-distinct files require a case-sensitive filesystem")
        self.assertEqual(self.submit()["error"], "duplicate_path")
        self.assertEqual(self.fx.remote_head(), "")

    def test_inherited_symlink_not_followed(self) -> None:
        outside = self.fx.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        try:
            os.symlink(outside, self.fx.seed / "input", target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation requires platform privileges")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "symlinked input", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        result = self.submit()
        self.assertIn(result["state"], {"published", "updated"})
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertIn("input/source.json", self.fx.remote_tree())

    def test_commit_hook_tamper_blocks_push(self) -> None:
        hook = self.fx.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            '#!/bin/sh\necho tampered >> "$(git rev-parse --show-toplevel)/urdf/robot.urdf"\n', encoding="utf-8"
        )
        hook.chmod(0o755)
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertIn(
            result["error"],
            {
                "commit_left_dirty",
                "committed_subject_mismatch",
                "reverification_failed",
                "reverification_binding_mismatch",
            },
        )
        self.assertEqual(self.fx.remote_head(), "")

    def test_same_cad_revision_allows_definition_changes(self) -> None:
        first = self.submit()
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))

    def test_same_label_changed_content_is_not_a_rejection(self) -> None:
        # Platform semantics: the CAD label is raw engineering metadata, never a
        # content identity; protection comes from the fixed base/head, the
        # published subject chain and CAS/ff, so a re-sealed label is allowed.
        first = self.submit()
        self.fx.seal("r1", None, cad="part-1-changed\n", fresh=True)
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))

    def test_new_revision_label_is_allowed(self) -> None:
        first = self.submit()
        self.fx.seal("r2", "r1", cad="part-2\n", fresh=True)
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        source = json.loads(run("git", "show", "FETCH_HEAD:input/source.json", cwd=self.fx.repo))
        self.assertEqual(source["metadata"]["revision"], "r2")
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))

    def test_missing_revision_label_publishes_with_content_identity(self) -> None:
        self.submit()
        self.fx.seal(None, None, cad="part-2\n", fresh=True)
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        source = json.loads(run("git", "show", "FETCH_HEAD:input/source.json", cwd=self.fx.repo))
        self.assertIsNone(source["metadata"]["revision"])
        previous = urdf_pr.published_revision(self.fx.repo, "m3.0")
        self.assertIsNone(previous["revision"])
        self.assertTrue(previous["source_revision_id"])

    def test_published_revision_unknown_is_not_absent(self) -> None:
        # A remote that answers "no such ref" is proven-absent; a transport or
        # read failure is unknown and must not masquerade as absence.
        self.assertIsNone(urdf_pr.published_revision(self.fx.repo, "m3.0"))
        failure = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="network is unreachable")
        real_run = subprocess.run

        def fake_run(*args, **kwargs):
            if "fetch" in args[0]:
                return failure
            return real_run(*args, **kwargs)

        with (
            mock.patch.object(urdf_pr.subprocess, "run", side_effect=fake_run),
            self.assertRaises(urdf_pr.PrError) as raised,
        ):
            urdf_pr.published_revision(self.fx.repo, "m3.0")
        self.assertEqual(raised.exception.code, "published_revision_unknown")


if __name__ == "__main__":
    unittest.main()
