"""The CLI exposes only the canonical StageApplication adapter commands."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock

from description_pipeline import __version__, cli
from description_pipeline.io import digest
from description_pipeline.pipeline.contracts import StageEffectWait, StagePending
from description_pipeline.schemas import STAGE_EFFECT_WAIT_SCHEMA


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

    def test_stage_pending_preserves_its_wait_without_retrying(self) -> None:
        wait = StageEffectWait(
            schema_version=STAGE_EFFECT_WAIT_SCHEMA,
            run_id="run-1",
            stage="capture",
            command_id="capture-command",
            request_digest=digest({"request": "capture"}),
            effect_id="capture-effect",
            effect_key="native",
            effect_kind="native_operation",
            intent_identity_digest=digest({"intent": "capture"}),
            outbound_artifact_id="capture-outbound",
            outbound_digest=digest({"outbound": "capture"}),
            expected_state_digest=digest({"expected": "capture"}),
        )
        application = Mock()
        application.execute_registered_command.return_value = StagePending(wait)
        factory = Mock(return_value=application)
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(
                ["stage", "capture-command"],
                application_factory=factory,
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "passed": False,
                "outcome": "pending",
                "wait": wait.to_record(),
            },
        )
        self.assertEqual(errors.getvalue(), "")
        factory.assert_called_once_with()
        application.execute_registered_command.assert_called_once_with(
            "capture-command"
        )


if __name__ == "__main__":
    unittest.main()
