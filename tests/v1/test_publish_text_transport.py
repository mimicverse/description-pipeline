"""Real process regressions for large publication text and shared warning budgets."""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.io import digest
from description_pipeline.pipeline.stages import publish as publish_stage
from description_pipeline.repository.publish import (
    GITHUB_PR_BODY_MAX_CHARACTERS,
    GitHubCliPullRequestClient,
    PublicationError,
    PublicationRepository,
)
from tests.v1.test_pipeline_publish import PublishFixture, git


class PublicationTextTransportTests(unittest.TestCase):
    def test_real_git_candidate_preserves_a_message_larger_than_exec_arguments(self) -> None:
        fixture = PublishFixture()
        self.addCleanup(fixture.close)
        inputs = fixture.handler._prepare(fixture.command, fixture.claim, fixture.workspace)
        message_body = "Original change summary\n参数 `literal` $(literal)\n" + "x" * 2_530_779
        specification = replace(inputs.request.commit, body=message_body)
        self.assertGreater(len(specification.message.encode("utf-8")), 2 * 1024**2)
        staging = fixture.root / "large-message-candidate"
        staging.mkdir()
        candidate = PublicationRepository(fixture.repo, expected_origin=str(fixture.remote)).candidate(
            publication_root=inputs.publication_root,
            base_ref=inputs.request.base_ref,
            expected_base_oid=inputs.request.expected_base_oid,
            branch_ref=inputs.request.branch_ref,
            expected_head_oid=inputs.request.expected_head_oid,
            commit_spec=specification,
            staging_root=staging,
        )
        self.addCleanup(candidate.cleanup)
        actual = git(candidate.worktree, "cat-file", "commit", candidate.commit).stdout.partition("\n\n")[2]
        self.assertEqual(actual, specification.message)
        self.assertEqual(candidate.subject_sha256, inputs.publication_subject_sha256)

    def test_real_gh_process_reads_full_multibyte_body_from_a_temporary_file(self) -> None:
        fixture = PublishFixture()
        self.addCleanup(fixture.close)
        bin_root = fixture.root / "text-gh-bin"
        bin_root.mkdir()
        executable = bin_root / "gh"
        executable.write_text(
            f"#!{sys.executable}\n" + '''import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["GH_TEXT_TEST_STATE"])
if args[:2] == ["pr", "create"]:
    body_path = Path(args[args.index("--body-file") + 1])
    row = {
        "number": 1, "url": "https://github.com/example/robot/pull/1", "state": "OPEN",
        "baseRefName": "main", "headRefName": "review/branch", "headRefOid": "a" * 40,
        "title": args[args.index("--title") + 1],
        "body": body_path.read_text(encoding="utf-8"), "body_path": str(body_path),
    }
    state.write_text(json.dumps(row), encoding="utf-8")
    print(row["url"])
elif args[:2] == ["pr", "view"]:
    print(state.read_text(encoding="utf-8"))
else:
    raise SystemExit("unexpected command")
''',
            encoding="utf-8",
        )
        executable.chmod(0o755)
        body = "电机模型\n" * 12_000
        self.assertLess(len(body), GITHUB_PR_BODY_MAX_CHARACTERS)
        self.assertGreater(len(body.encode("utf-8")), 131_072)
        state = fixture.root / "text-gh-state.json"
        with patch.dict(os.environ, {
            "PATH": str(bin_root) + os.pathsep + os.environ["PATH"],
            "GH_TEXT_TEST_STATE": str(state),
        }):
            observation = GitHubCliPullRequestClient(fixture.repo).create(
                repository="example/robot", base_ref="main", head_ref="review/branch",
                candidate_commit="a" * 40, title="Publish verified model", body=body,
            )
        self.assertEqual(observation.body, body)
        self.assertFalse(Path(json.loads(state.read_text())["body_path"]).exists())


class PublicationTextBudgetTests(unittest.TestCase):
    @staticmethod
    def mjcf_certificate(warnings: list[str]) -> dict:
        return {"report": {"checks": [{"id": "provenance.warnings", "details": {
            "warning_comments": warnings, "warning_sha256": digest(warnings),
        }}]}}

    @staticmethod
    def request(summary: str):
        return SimpleNamespace(
            commit=SimpleNamespace(body=summary, subject="Publish verified model"),
            repository_slug="example/robot", mjcf_certificate_sha256="c" * 64,
        )

    def test_mixed_megabyte_warning_inventories_share_one_body_capacity(self) -> None:
        certificate = {"diagnostics": [
            {"code": "source.recorded", "blocking": False, "object": f"part-{index}",
             "detail": {"source": "x" * 2000, "hardware_qualified": False}}
            for index in range(100)
        ]}
        warnings = ["WARNING mjcf.collision_proxy: " + json.dumps({
            "object": f"mesh-{index}", "source": "x" * 900, "hardware_qualified": False,
        }) for index in range(3000)]
        self.assertGreater(sum(len(item) for item in warnings), 2_000_000)
        mjcf_certificate = self.mjcf_certificate(warnings)
        original = copy.deepcopy((certificate, mjcf_certificate))
        summary = "完整变更说明\nKeep the original multiline description."
        _, body = publish_stage._publication_text(
            self.request(summary), certificate=certificate, certificate_sha256="a" * 64,
            subject_sha256="b" * 64, mjcf_certificate=mjcf_certificate,
        )
        self.assertTrue(body.startswith(summary + "\nVerification warnings (100 recorded)"))
        self.assertLessEqual(len(body), GITHUB_PR_BODY_MAX_CHARACTERS)
        self.assertIn("MJCF projection warnings (3000 recorded)", body)
        self.assertIn("additional warning records", body)
        self.assertIn("reports/verification-certificate.json", body)
        self.assertIn("reports/mjcf-certificate.json", body)
        self.assertEqual((certificate, mjcf_certificate), original)

    def test_many_warning_categories_and_large_first_records_remain_bounded(self) -> None:
        certificate = {"diagnostics": [
            {"code": f"source.category_{index}", "blocking": False,
             "detail": {"source": "x" * 9000}}
            for index in range(600)
        ]}
        body = publish_stage._publication_warning_text(certificate, max_characters=2000)
        self.assertLessEqual(len(body), 2000)
        self.assertIn("warning categories are not expanded", body)
        self.assertIn("additional warning records", body)

    def test_oversized_required_description_is_rejected_without_silent_truncation(self) -> None:
        summary = "x" * GITHUB_PR_BODY_MAX_CHARACTERS
        with self.assertRaises(PublicationError) as raised:
            publish_stage._publication_text(
                self.request(summary), certificate={"diagnostics": []},
                certificate_sha256="a" * 64, subject_sha256="b" * 64,
                mjcf_certificate=self.mjcf_certificate([]),
            )
        self.assertEqual(raised.exception.code, "publish.pr_text_capacity")
        self.assertEqual(summary, "x" * GITHUB_PR_BODY_MAX_CHARACTERS)


if __name__ == "__main__":
    unittest.main()
