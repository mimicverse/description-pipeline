"""CLI maintenance boundaries: no standalone publish, no in-delivery report writes (R20)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline import cli
from description_pipeline.io import PipelineError


class CliBoundaryTests(unittest.TestCase):
    def test_standalone_submit_subcommand_is_gone(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            cli.parser().parse_args(["submit", "bundle", "--repository", "repo"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_rebuild_accepts_no_publish_arguments(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            cli.parser().parse_args(["rebuild", "bundle", "--output", "out", "--repository", "repo", "--base", "main"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_in_delivery_report_write_is_refused_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delivery = Path(tmp) / "delivery"
            (delivery / "reports").mkdir(parents=True)
            sealed = delivery / "reports/quality.json"
            sealed.write_text('{"passed": true}\n', encoding="utf-8")
            before = sealed.read_bytes()
            with self.assertRaisesRegex(PipelineError, "outside the delivery"):
                cli._write_report(sealed, {"passed": False}, delivery)
            self.assertEqual(sealed.read_bytes(), before)

    def test_external_report_write_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delivery = Path(tmp) / "delivery"
            (delivery / "reports").mkdir(parents=True)
            target = Path(tmp) / "recomputed.json"
            cli._write_report(target, {"passed": True}, delivery)
            import json

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"passed": True})
