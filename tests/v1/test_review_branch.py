"""Pure review-branch naming rules used by Publish."""

import unittest

from description_pipeline.repository.review_branch import (
    ReviewBranchError,
    is_review_branch,
    review_branch,
)


class ReviewBranchTests(unittest.TestCase):
    def test_engineering_and_inspection_branches_are_canonical(self) -> None:
        self.assertEqual(review_branch("M3.0", "engineering"), "work/solidworks/m3.0/engineering")
        self.assertEqual(
            review_branch("m3.0", "inspection_preview"),
            "work/solidworks/m3.0/inspection_preview",
        )
        self.assertTrue(is_review_branch("work/solidworks/m3.0/engineering"))
        self.assertTrue(is_review_branch("work/solidworks/m3.0/inspection_preview"))

    def test_noncanonical_and_purpose_unknown_branches_are_rejected(self) -> None:
        for value in (
            "main",
            "refs/heads/main",
            "feature/m3.0",
            "work/solidworks/M3.0/engineering",
            "work/solidworks/m3.0/unknown",
            "work/solidworks/m3.0/engineering/extra",
            None,
        ):
            self.assertFalse(is_review_branch(value), value)
        with self.assertRaises(ReviewBranchError):
            review_branch("m3.0", "unknown")


if __name__ == "__main__":
    unittest.main()
