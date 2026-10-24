"""The CLI exposes only the canonical StageApplication adapter commands."""

from __future__ import annotations

import unittest

from description_pipeline import cli


class CliBoundaryTests(unittest.TestCase):
    def test_stage_application_commands_are_exposed(self) -> None:
        self.assertEqual(cli.parser().parse_args(["admit", "spec.json"]).operation, "admit")
        self.assertEqual(cli.parser().parse_args(["stage", "command-1"]).operation, "stage")
        self.assertEqual(cli.parser().parse_args(["run", "run-1"]).operation, "run")
        self.assertEqual(cli.parser().parse_args(["doctor"]).operation, "doctor")

    def test_legacy_standalone_entries_are_absent(self) -> None:
        for argv in (
            ["submit", "bundle", "--repository", "repo"],
            ["rebuild", "bundle", "--output", "out"],
            ["check", "bundle"],
            ["serve", "--config", "config.json"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                cli.parser().parse_args(argv)

    def test_publish_options_are_not_accepted_by_the_run_adapter(self) -> None:
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(["run", "run-1", "--repository", "repo"])


if __name__ == "__main__":
    unittest.main()
