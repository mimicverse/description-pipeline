import contextlib
import io
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError
from description_pipeline.repository import git
from tools import build_release


class ReleaseBuildTests(unittest.TestCase):
    def test_the_build_backend_is_pinned_to_the_lock(self):
        """A floating builder changes the wheel's bytes without changing a single source file."""

        root = Path(__file__).resolve().parents[1]
        requires = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]["requires"]
        pinned = [entry for entry in requires if entry.startswith("setuptools==")]
        self.assertEqual(len(pinned), 1, "the isolated build must pin exactly one setuptools version")
        lock = (root / "requirements" / "linux-py312.lock").read_text(encoding="utf-8")
        self.assertIn(pinned[0], lock, "the pinned builder has to be the one the lock installs")
        # An old builder is not only a reproducibility problem: MANIFEST.in exclusions became
        # bypassable below 83.0.0 (CVE-2026-59890).
        version = pinned[0].split("==", 1)[1]
        self.assertGreaterEqual(tuple(int(part) for part in version.split(".")[:2]), (83, 0))

    def test_a_different_builder_is_refused(self):
        root = Path(__file__).resolve().parents[1]
        with (
            patch("importlib.metadata.version", return_value="999.0.0"),
            self.assertRaisesRegex(PipelineError, "the builder version is part of the artifact bytes"),
        ):
            build_release.check_build_environment(root)

    def test_a_missing_builder_is_refused(self):
        import importlib.metadata

        root = Path(__file__).resolve().parents[1]
        missing = importlib.metadata.PackageNotFoundError("setuptools")
        with (
            patch.object(importlib.metadata, "version", side_effect=missing),
            self.assertRaisesRegex(PipelineError, "install requirements/linux-py312.lock"),
        ):
            build_release.check_build_environment(root)

    def test_nonempty_destination_is_preserved_before_building(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            artifact = output / "existing.whl"
            artifact.write_bytes(b"previous delivery")
            with (
                patch("sys.argv", ["build_release.py", "--out", str(output)]),
                self.assertRaisesRegex(PipelineError, "output must be empty"),
            ):
                build_release.main()
            self.assertEqual(artifact.read_bytes(), b"previous delivery")
            self.assertEqual(list(output.iterdir()), [artifact])

    def test_clean_package_does_not_excuse_a_modified_release_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "tools/build_release.py"
            script.parent.mkdir()
            script.write_text("committed release logic")
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Fixture")
            git(root, "config", "user.email", "fixture@example.invalid")
            git(root, "add", ".")
            git(root, "commit", "-m", "tool release")
            sha = git(root, "rev-parse", "HEAD").stdout.strip()
            script.write_text("uncommitted release logic")
            with (
                patch.object(build_release, "__file__", str(script)),
                patch.object(
                    build_release.description_pipeline, "__file__", str(root / "src/description_pipeline/__init__.py")
                ),
                patch.object(build_release, "tool_identity", return_value={"development": False, "source_commit": sha}),
                patch("sys.argv", ["build_release.py", "--require-clean", "--out", str(root / "dist")]),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(PipelineError, "clean committed checkout"),
            ):
                build_release.main()
            self.assertFalse((root / "dist").exists())
