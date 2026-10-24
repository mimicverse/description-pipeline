"""The CLI exposes only the canonical StageApplication adapter commands."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from description_pipeline import __version__, cli


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

    def test_version_banner_has_one_installable_program_identity(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"{cli.CLI_VERSION_PREFIX} {__version__}")


if __name__ == "__main__":
    unittest.main()
