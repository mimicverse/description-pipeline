"""Bounded text inventory views retain the sealed certificate as their authority."""

import copy
import unittest

from description_pipeline.io import PipelineError, canonical, digest
from description_pipeline.orchestration.report_view import text_warning_inventory


class TextWarningInventoryTests(unittest.TestCase):
    def setUp(self):
        self.member = {
            "artifact": {"artifact_id": "a" * 64}, "path": "reports/mjcf-certificate.json",
            "bytes": 4000000, "sha256": "b" * 64,
        }

    def test_large_unicode_inventory_is_bounded_without_changing_records(self):
        records = [f"WARNING source.example: {index} " + ("source" + chr(0x1F642)) * 300 for index in range(1300)]
        before = copy.deepcopy(records)
        summary = text_warning_inventory(records, self.member)
        self.assertLessEqual(len(canonical(summary)), 32768)
        self.assertEqual(summary["count"], len(records))
        self.assertEqual(summary["sha256"], digest(records))
        self.assertEqual(summary["shown"], len(summary["items"]))
        self.assertEqual(summary["omitted"] + summary["shown"], len(records))
        self.assertGreater(summary["omitted"], 0)
        self.assertTrue(all(item["excerpt"] for item in summary["items"]))
        self.assertTrue(all(item["text"].endswith("[excerpt; see certificate]") for item in summary["items"]))
        self.assertEqual(records, before)
        self.assertEqual(summary["certificate"], self.member)
        summary["certificate"]["artifact"]["artifact_id"] = "c" * 64
        self.assertEqual(self.member["artifact"]["artifact_id"], "a" * 64)

    def test_small_records_are_complete_and_identity_must_fit(self):
        records = ["WARNING source.first: preserved", "WARNING source.second: preserved"]
        summary = text_warning_inventory(records, self.member)
        self.assertEqual(summary["items"], [{"text": value, "excerpt": False} for value in records])
        self.assertEqual(summary["omitted"], 0)
        with self.assertRaises(PipelineError):
            text_warning_inventory(records, self.member, max_bytes=1)
