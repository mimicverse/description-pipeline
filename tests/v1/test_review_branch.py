"""Pure review-branch naming rules used by Publish."""

import unittest

from description_pipeline.repository.review_branch import (
    ReviewBranchError,
    is_review_branch,
    review_branch,
)


class ReviewBranchTests(unittest.TestCase):
    def test_request_branches_are_canonical_and_deterministic(self) -> None:
        branch = review_branch("M3.0", "run-1", "request-1")
        self.assertRegex(
            branch,
            r"work/description/m3\.0/[0-9a-f]{24}",
        )
        self.assertEqual(
            branch,
            review_branch("m3.0", "run-1", "request-1"),
        )
        self.assertNotEqual(
            branch,
            review_branch("m3.0", "run-2", "request-1"),
        )
        self.assertTrue(is_review_branch(branch))

    def test_noncanonical_branches_are_rejected(self) -> None:
        for value in (
            "main",
            "refs/heads/main",
            "feature/m3.0",
            "work/description/M3.0/0123456789abcdef01234567",
            "work/description/m3.0/not-a-request-digest",
            "work/description/m3.0/0123456789abcdef01234567/extra",
            None,
        ):
            self.assertFalse(is_review_branch(value), value)
        with self.assertRaises(ReviewBranchError):
            review_branch("m3.0", "", "request-1")


if __name__ == "__main__":
    unittest.main()
