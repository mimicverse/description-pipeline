"""The public CLI must report the real cause of a failing subprocess.

The Windows capture hits this first: ``git add`` fails with ``git-lfs: not found``
in a non-interactive SSH shell, and all the operator sees is what the JSON message
keeps - ``str(CalledProcessError)`` alone names the command and exit status but
throws the stderr away, so the cause had to be reproduced by hand.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline import cli
from description_pipeline.repository import git


def run_cli(argv: list[str]) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli.main(argv)
    # Success and failure reports carry `next:` hints that the CLI prints to stderr before the report
    # itself; both streams land in this one buffer, so the report starts at its first brace.
    text = buffer.getvalue()
    return code, json.loads(text[text.index("{") :])


class CliFailureMessageTests(unittest.TestCase):
    def test_a_directory_that_is_not_a_workspace_says_so(self):
        """The first mistake a new operator makes is running a workspace command in the wrong place."""

        with tempfile.TemporaryDirectory() as folder:
            code, report = run_cli(["check", "--root", folder, "--profile", "kinematics"])
        self.assertNotEqual(code, 0)
        self.assertIn("Not a model workspace", report["message"])
        self.assertIn("config/robot.yaml", report["message"])
        self.assertNotIn("Expected one explicit profile", report["message"])

    def test_an_advisory_is_printed_before_the_report(self):
        """A review branch that no longer contains its base has to say so in the transcript.

        The one-click launchers print whatever the tool prints, so an advisory that only lives
        inside the JSON would never reach the operator who needs it.
        """

        advisory = {
            "code": "review_branch_behind_base",
            "message": "this review branch does not contain the current feature/microban",
            "commands": ["git fetch origin", "git merge origin/feature/microban"],
            "then": "description model submit --root . --profile kinematics --message-file -",
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(cli, "submit", return_value={"passed": True, "advisories": [advisory]}),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = cli.main(["model", "submit", "--root", folder, "--profile", "kinematics", "--message", "x"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("note: this review branch does not contain the current feature/microban", text)
        self.assertIn("git fetch origin", text)
        self.assertIn("git merge origin/feature/microban", text)
        self.assertIn("description model submit --root . --profile kinematics --message-file -", text)

    def test_an_unaccepted_application_names_the_command_that_records_it(self):
        """`consumer.application` is a not-run check, so "fix the blockers" says nothing useful."""

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(cli, "assess", return_value={"passed": False, "blockers": ["consumer.application"]}):
                code, payload = run_cli(["check", "--root", folder, "--profile", "simulation"])
            self.assertEqual(code, 1)
            hint = next(item for item in payload["next"] if "model accept" in item)
            self.assertIn(f"--root {folder}", hint)
            self.assertIn("--profile simulation", hint)
            self.assertIn("--out", hint)

    def test_other_blockers_keep_the_plain_hint(self):
        """The control: a failure that is not about the acceptance must not suggest running it."""

        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(cli, "assess", return_value={"passed": False, "blockers": ["bundle.identity"]}):
                code, payload = run_cli(["check", "--root", folder, "--profile", "simulation"])
            self.assertEqual(code, 1)
            self.assertEqual(
                payload["next"], ["fix the blockers above (or the files they name), then run this command again"]
            )

    def test_a_hardware_build_is_not_sent_to_the_simulation_acceptance(self):
        """`model accept` records `docs/acceptance/simulation.json`; a hardware build needs other evidence."""

        with tempfile.TemporaryDirectory() as folder:
            profiles = Path(folder) / "config" / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "hardware.json").write_text(json.dumps({"purpose": "hardware"}), encoding="utf-8")
            with mock.patch.object(cli, "assess", return_value={"passed": False, "blockers": ["consumer.application"]}):
                code, payload = run_cli(["check", "--root", folder, "--profile", "hardware"])
            self.assertEqual(code, 1)
            hint = next(item for item in payload["next"] if "does not record" in item)
            self.assertIn("hardware", hint)
            self.assertIn("docs/validation.md", hint)
            self.assertNotIn("run `description model accept --root", hint)

    def test_a_simulation_build_points_at_the_acceptance_as_well(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(cli, "build", return_value={"passed": False, "blockers": ["consumer.application"]}):
                code, payload = run_cli(["build", "--root", folder, "--profile", "simulation"])
            self.assertEqual(code, 1)
            self.assertTrue(any("model accept" in item for item in payload["next"]))
            self.assertTrue(any(item.startswith("description check") for item in payload["next"]))

    def test_subprocess_stderr_reaches_the_message_and_the_diagnostic(self):
        failure = subprocess.CalledProcessError(
            128, ["git", "-C", "model", "add", "--", "."], stderr="git-lfs: not found\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            report = base / "reports" / "build.json"
            with mock.patch.object(cli, "build", side_effect=failure):
                code, payload = run_cli(
                    ["build", "--root", str(base / "model"), "--profile", "kinematics", "--report", str(report)]
                )

            self.assertEqual(code, 2)
            self.assertFalse(payload["passed"])
            self.assertEqual(payload["error"], "CalledProcessError")
            self.assertIn("returned non-zero exit status 128", payload["message"])
            self.assertIn("git-lfs: not found", payload["message"])
            self.assertNotIn("\n", payload["message"])
            written = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(written, payload)

    def test_a_failure_without_stderr_keeps_the_plain_message(self):
        failure = subprocess.CalledProcessError(1, ["gh", "api", "repos/org/repo"])
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(cli, "build", side_effect=failure):
            code, payload = run_cli(["build", "--root", folder, "--profile", "kinematics"])

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "CalledProcessError")
        self.assertEqual(payload["message"], str(failure))
        self.assertFalse(payload["message"].endswith(": "))


class CliInputErrorTests(unittest.TestCase):
    """Operator mistakes must name the fix, not the failing subprocess."""

    def test_init_reports_a_missing_source_config(self):
        with tempfile.TemporaryDirectory() as folder:
            code, payload = run_cli(
                [
                    "model",
                    "init",
                    "--root",
                    str(Path(folder) / "model"),
                    "--hardware",
                    "demo",
                    "--source-config",
                    str(Path(folder) / "missing.yaml"),
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "PipelineError")
        self.assertIn("Source config not found", payload["message"])
        self.assertIn("missing.yaml", payload["message"])

    def test_diff_reports_a_directory_that_is_not_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            code, payload = run_cli(["diff", "HEAD", folder, "--repository", folder])

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "PipelineError")
        self.assertIn("not a Git checkout", payload["message"])
        self.assertIn("--repository", payload["message"])

    def test_diff_reports_an_unresolvable_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            git(root, "init", "-b", "main")
            git(root, "config", "user.email", "fixture@example.invalid")
            git(root, "config", "user.name", "Fixture")
            (root / "README.md").write_text("# fixture\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "fixture")
            code, payload = run_cli(["diff", "deadbeef", str(root), "--repository", str(root)])

        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "PipelineError")
        self.assertIn("Cannot resolve deadbeef", payload["message"])
        self.assertIn("fatal:", payload["message"])


if __name__ == "__main__":
    unittest.main()
