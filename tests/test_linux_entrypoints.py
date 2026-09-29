"""The Linux distribution launchers are small, shell-valid and package data."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ROOT / "src/description_pipeline/deploy/linux"
#: A Windows host without Git for Windows has no ``bash``; the launchers only run on Linux, so the
#: syntax check is a skip there rather than an error.
BASH = shutil.which("bash")


class LinuxEntrypointTests(unittest.TestCase):
    @unittest.skipUnless(BASH, "shell syntax check needs bash on PATH")
    def test_launchers_are_shell_valid(self):
        bash = BASH or "bash"
        for name in ("install.sh", "submit.sh"):
            with self.subTest(name=name):
                path = LAUNCHERS / name
                self.assertTrue(path.is_file())
                result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_data_declares_linux_launchers(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"deploy/linux/*.sh"', pyproject)

    @unittest.skipUnless(BASH, "the launchers are bash scripts")
    def test_install_script_explains_how_to_recover(self):
        """A failed first installation names the fix, not only the symptom."""

        bash = BASH or "bash"
        with tempfile.TemporaryDirectory(prefix="description-install-test-") as directory:
            bundle = Path(directory)
            (bundle / "wheels").mkdir()
            (bundle / "requirements.lock").write_text("# pinned\n", encoding="utf-8")
            shutil.copy(LAUNCHERS / "install.sh", bundle / "install.sh")
            # Stands in for an interpreter without ensurepip and for a pip download that fails.
            fake = bundle / "fake-python"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *"-m venv"*) echo "ensurepip is not available" >&2; exit 1 ;;\n'
                '  *"-m pip"*) echo "No space left on device" >&2; exit 1 ;;\n'
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            environment = {**os.environ, "DESCRIPTION_PYTHON": str(fake)}

            failed = subprocess.run(
                [bash, str(bundle / "install.sh")], cwd=bundle, capture_output=True, text=True, env=environment
            )
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("python3.12-venv", failed.stderr, "a missing venv module must name the package")

            venv_python = bundle / "venv/bin/python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_bytes(fake.read_bytes())
            venv_python.chmod(0o755)
            environment["DESCRIPTION_VENV"] = str(bundle / "venv")
            failed = subprocess.run(
                [bash, str(bundle / "install.sh")], cwd=bundle, capture_output=True, text=True, env=environment
            )
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertIn("wheels/", failed.stderr, "a failed dependency install must name what to check")

    @unittest.skipUnless(BASH, "the launchers are bash scripts")
    def test_an_existing_venv_wins_over_the_path_interpreter(self):
        """A shared 3.12 runtime installs even when ``python3`` on PATH is newer.

        The bundle documents ``DESCRIPTION_VENV`` for shared or read-only deployments, and a distro
        upgrade moves ``python3`` past 3.12; neither may break a venv that is already correct.
        """

        bash = BASH or "bash"

        def interpreter(path: Path, version: str, *, pip: bool) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"--version"*) echo "Python {version}" ;;\n'
                '  *"-c"*) ' + ("exit 0" if version.startswith("3.12") else "exit 1") + " ;;\n"
                '  *"-m pip"*) ' + ("exit 0" if pip else "exit 1") + " ;;\n"
                "esac\n"
                "exit 1\n",
                encoding="utf-8",
            )
            path.chmod(0o755)

        with tempfile.TemporaryDirectory(prefix="description-install-test-") as directory:
            bundle = Path(directory)
            (bundle / "wheels").mkdir()
            (bundle / "requirements.lock").write_text("# pinned\n", encoding="utf-8")
            shutil.copy(LAUNCHERS / "install.sh", bundle / "install.sh")
            bindir = bundle / "path-bin"
            interpreter(bindir / "python3", "3.13.7", pip=False)
            interpreter(bindir / "python3.12", "3.13.7", pip=False)
            environment = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}

            venv = bundle / "shared-venv"
            interpreter(venv / "bin/python", "3.12.14", pip=True)
            environment["DESCRIPTION_VENV"] = str(venv)
            installed = subprocess.run(
                [bash, str(bundle / "install.sh")], cwd=bundle, capture_output=True, text=True, env=environment
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertIn(f"Installed description into: {venv}", installed.stdout)

            outdated = bundle / "old-venv"
            interpreter(outdated / "bin/python", "3.11.9", pip=True)
            environment["DESCRIPTION_VENV"] = str(outdated)
            rejected = subprocess.run(
                [bash, str(bundle / "install.sh")], cwd=bundle, capture_output=True, text=True, env=environment
            )
            self.assertEqual(rejected.returncode, 2, rejected.stdout)
            self.assertIn("Existing virtual environment is not CPython 3.12", rejected.stderr)

            # Without a venv the PATH interpreter is still validated before anything is created.
            environment.pop("DESCRIPTION_VENV")
            missing = subprocess.run(
                [bash, str(bundle / "install.sh")], cwd=bundle, capture_output=True, text=True, env=environment
            )
            self.assertEqual(missing.returncode, 2, missing.stdout)
            self.assertIn("CPython 3.12 is required; found Python 3.13.7", missing.stderr)
            self.assertFalse((bundle / ".venv").exists(), "no runtime may be created for a rejected interpreter")


if __name__ == "__main__":
    unittest.main()
