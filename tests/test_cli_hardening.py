"""Hostile input ends as a diagnostic, never as a traceback.

A user who mistypes a path, deletes a file or edits a lock by hand is exactly who the CLI has to
help.  ``tests/test_doctor.py`` covers the doctor report; this covers the commands that build and
check a model, where a non-object lock used to raise ``AttributeError`` and a deleted artifact used
to raise a bare ``FileNotFoundError`` out of the command that was supposed to explain it.
"""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from description_pipeline import cli
from description_pipeline.build import build, freeze, lock_toolchain

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")
#: ``description diff`` resolves revisions through Git; a source archive has no checkout to offer.
GIT = shutil.which("git")


def run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


class HostileInputTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]
    workspace: Path

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.temporary.name) / "demo-arm"
        shutil.copytree(EXAMPLE, cls.workspace, ignore=IGNORED)
        lock_toolchain(cls.workspace)
        freeze(cls.workspace)
        build(cls.workspace, "kinematics")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def case(self, directory: str, mutate: Callable[[Path], None]) -> Path:
        target = Path(directory) / "case"
        shutil.copytree(self.workspace, target)
        mutate(target)
        return target

    def assert_diagnostic(self, code: int, output: str, *expected: str) -> dict:
        self.assertNotEqual(code, 0, output)
        self.assertNotIn("Traceback", output, output)
        payload = json.loads(output)
        self.assertEqual(payload["error"], "PipelineError", payload)
        for fragment in expected:
            self.assertIn(fragment, payload["message"], payload)
        return payload

    def test_a_non_object_toolchain_lock_is_reported(self):
        for text in ("[]", '"x"'):

            def mutate(path: Path, text: str = text) -> None:
                (path / "config/toolchain.lock.json").write_text(text)

            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                target = self.case(directory, mutate)
                for command in ("build", "check"):
                    code, output = run_cli([command, "--root", str(target)])
                    self.assert_diagnostic(code, output, "config/toolchain.lock.json", "description tool lock")

    def test_a_broken_source_lock_is_reported(self):
        def missing_lock(path: Path) -> None:
            (path / "sources/source.lock.json").unlink()

        def missing_directory(path: Path) -> None:
            shutil.rmtree(path / "sources")

        def broken_json(text: str) -> Callable[[Path], None]:
            def mutate(path: Path) -> None:
                (path / "sources/source.lock.json").write_text(text)

            return mutate

        mutations: list[tuple[str, Callable[[Path], None]]] = [
            ("list", broken_json("[]")),
            ("string", broken_json('"x"')),
            ("missing file", missing_lock),
            ("missing directory", missing_directory),
        ]
        for label, mutate in mutations:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                target = self.case(directory, mutate)
                code, output = run_cli(["build", "--root", str(target)])
                self.assert_diagnostic(code, output, "sources/source.lock.json", "description source freeze")

    def test_a_deleted_model_artifact_names_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self.case(directory, lambda path: shutil.rmtree(path / "model"))
            code, output = run_cli(["check", "--root", str(target)])
        self.assert_diagnostic(code, output, "model/robot.json")

    def test_a_damaged_bundle_artifact_is_reported_by_check(self):
        cases = {
            "manifest list": ("manifest.json", "[]"),
            "manifest reports list": ("manifest.json", '{"reports": []}'),
            "quality list": ("docs/quality.json", "[]"),
            "quality string": ("docs/quality.json", '"x"'),
        }
        for label, (relative, text) in cases.items():

            def mutate(path: Path, relative: str = relative, text: str = text) -> None:
                (path / relative).write_text(text, encoding="utf-8")

            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                target = self.case(directory, mutate)
                code, output = run_cli(["check", "--root", str(target)])
                self.assert_diagnostic(code, output, relative, "description build")

    @unittest.skipUnless(GIT, "the diff check needs Git on PATH")
    def test_a_damaged_candidate_is_reported_by_diff(self):
        cases = {
            "robot list": ("model/robot.json", "[]"),
            "robot without fields": ("model/robot.json", '{"links": []}'),
            "quality list": ("docs/quality.json", "[]"),
        }
        for label, (relative, text) in cases.items():

            def mutate(path: Path, relative: str = relative, text: str = text) -> None:
                (path / relative).write_text(text, encoding="utf-8")

            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                target = self.case(directory, mutate)
                # A throwaway repository keeps this test working in a source archive, where
                # the checkout under test is not itself a Git repository.
                repository = Path(directory) / "repo"
                repository.mkdir()
                subprocess.run([GIT or "git", "init", "--quiet", str(repository)], check=True, capture_output=True)
                code, output = run_cli(["diff", "--repository", str(repository), str(target), str(target)])
                self.assert_diagnostic(code, output, relative, "description build")

    def test_a_root_that_is_a_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            not_a_directory = Path(directory) / "robot.yaml"
            not_a_directory.write_text("not a workspace\n", encoding="utf-8")
            for command in ("build", "check", "doctor"):
                with self.subTest(command=command):
                    code, output = run_cli([command, "--root", str(not_a_directory)])
                    self.assert_diagnostic(code, output, "--root must be a directory")

    def test_tool_lock_refuses_a_directory_that_is_not_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            code, output = run_cli(["tool", "lock", "--root", str(empty)])
            self.assert_diagnostic(code, output, "config/robot.yaml")
            self.assertFalse((empty / "config").exists(), "a rejected run must not write anything")


if __name__ == "__main__":
    unittest.main()
