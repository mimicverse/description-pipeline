"""The deployment surface is part of the contract, so it gets regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import deploy as deploy_resources

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
WORKER_PS1 = deploy_resources.resource_path(deploy_resources.WORKER_SCRIPT)
HOST_EXAMPLE = deploy_resources.resource_path(deploy_resources.HOST_TEMPLATE)
BUILD_BUNDLE = deploy_resources.resource_path(deploy_resources.BUNDLE_BUILDER)
PACKAGE_DATA_KEYS = (
    "sources/solidworks/deploy/*.ps1",
    "sources/solidworks/deploy/*.json",
    "sources/solidworks/deploy/requirements/*.lock",
)


def update_expectation_problems(text: str) -> list[str]:
    """The update path has to take the new archive's digest from the release, not from the install.

    Verified natively first: with ``worker-host.json``'s digest as the expectation, `-Action Update`
    refused the 0.3.18 archive while 0.3.17 was installed ("bundle digest mismatch: expected
    1d1f437c…, got 634a6497…") — which is what the launcher's own usage text told an operator to run.
    """

    update = text[text.index("'Update' {") :]
    update = update[: update.index("'Rollback' {")]
    problems: list[str] = []
    if "Resolve-BundleDigest $Bundle $BundleSha256 -Strict" not in update:
        problems.append("the update path does not resolve the new archive's digest from its release")
    if "$host_.bundle_sha256" in update:
        problems.append("the update path expects the digest of the version that is installed")
    return problems


class PackagedResourceTests(unittest.TestCase):
    """The deploy resources ship as package resources, not as checkout files."""

    def test_resources_live_inside_the_package(self) -> None:
        for path in (
            WORKER_PS1,
            HOST_EXAMPLE,
            deploy_resources.resource_path(deploy_resources.SUBMIT_SCRIPT),
            deploy_resources.resource_path(deploy_resources.SUBMIT_TEMPLATE),
            BUILD_BUNDLE,
            deploy_resources.resource_path(deploy_resources.LOCK_FILE),
        ):
            self.assertTrue(path.is_file(), path)
            self.assertIn("description_pipeline", path.parts)
            self.assertGreater(path.stat().st_size, 0)

    def test_package_data_declares_every_resource_kind(self) -> None:
        text = PYPROJECT.read_text(encoding="utf-8")
        for key in PACKAGE_DATA_KEYS:
            self.assertIn(key, text, key)

    def test_export_writes_the_operator_visible_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = deploy_resources.export_deploy_resources(Path(tmp))

            names = {path.name for path in written}
            for expected in (
                "worker.ps1",
                "worker-host.example.json",
                "submit.ps1",
                "submit-host.example.json",
                "build_bundle.py",
                "win-py312.lock",
            ):
                self.assertIn(expected, names)
            self.assertTrue((Path(tmp) / "requirements" / "win-py312.lock").is_file())
            self.assertIn("pywin32==", (Path(tmp) / "requirements" / "win-py312.lock").read_text(encoding="utf-8"))

    def test_packaged_lock_matches_what_the_cli_prints(self) -> None:
        entries = deploy_resources.lock_requirements()
        self.assertIn("pywin32==311", entries)
        result = subprocess.run(
            [sys.executable, "-m", "description_pipeline.sources.solidworks.deploy", "requirements"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip().splitlines(), entries)


class WorkerScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = WORKER_PS1.read_text(encoding="utf-8")

    def test_exposes_every_required_action(self) -> None:
        for action in ("Install", "Start", "Doctor", "Update", "Rollback"):
            self.assertIn(f"'{action}'", self.script, action)

    def test_registers_an_interactive_single_instance_task(self) -> None:
        self.assertIn("-LogonType Interactive", self.script)
        self.assertIn("-MultipleInstances IgnoreNew", self.script)

    def test_never_ends_solidworks_or_deletes_the_install_root(self) -> None:
        # Worker PID isolation and busy refusal run in tests/windows/test_deployment.ps1.
        self.assertNotIn("Stop-Process -Name", self.script)
        self.assertNotIn("Remove-Item -LiteralPath $host_.install_root -Recurse", self.script)
        self.assertIn('Join-Path $InstallRoot "versions\\$version"', self.script)

    def test_only_a_verified_idle_worker_pid_is_stopped(self) -> None:
        # v12: cleanup may only touch this installation's own, idle worker
        self.assertIn('Get-CimInstance Win32_Process -Filter "ProcessId=$($health.pid)"', self.script)
        self.assertIn("refusing to stop a process outside this versioned worker installation", self.script)
        self.assertIn("worker has active or queued work", self.script)
        self.assertIn("Invoke-WorkerEndpoint $ConfigHost '/maintenance' 'POST'", self.script)
        self.assertNotIn("Stop-Process -Name", self.script)
        self.assertNotIn("SLDWORKS.exe", self.script.replace("SLDWORKS is never targeted", ""))

    def test_upgrade_is_idle_gated_and_rolls_back_on_failure(self) -> None:
        activate = self.script[self.script.index("function Activate-Version") :]
        stop_at = activate.index("Stop-Worker $ConfigHost")
        start_at = activate.index("Start-Worker $host".replace("$host", "$ConfigHost"))
        pointer_at = activate.index("Set-CurrentVersion")
        rollback_at = activate.index("restored previous worker")
        # stop the old (idle) worker, start the new one, and only then move the pointer
        self.assertLess(stop_at, start_at)
        self.assertLess(start_at, pointer_at)
        self.assertLess(pointer_at, rollback_at)

    def test_update_takes_the_new_archive_from_its_own_release(self) -> None:
        self.assertEqual(update_expectation_problems(self.script), [])

    def test_the_update_expectation_check_reports_the_installed_digest(self) -> None:
        """The control: the exact line the native run showed failing."""

        broken = self.script.replace(
            "$expected = Resolve-BundleDigest $Bundle $BundleSha256 -Strict",
            "$expected = if ($BundleSha256) { $BundleSha256 } else { $host_.bundle_sha256 }",
        )
        self.assertEqual(
            update_expectation_problems(broken),
            [
                "the update path does not resolve the new archive's digest from its release",
                "the update path expects the digest of the version that is installed",
            ],
        )

    def test_host_config_example_is_valid_json_with_required_keys(self) -> None:
        payload = json.loads(HOST_EXAMPLE.read_text(encoding="utf-8"))
        for key in ("install_root", "user", "port", "task_name"):
            self.assertIn(key, payload)
        self.assertEqual(payload["host"], "127.0.0.1")


class LockFileTests(unittest.TestCase):
    def test_windows_lock_covers_the_public_core_and_the_com_boundary(self) -> None:
        lock = deploy_resources.resource_path(deploy_resources.LOCK_FILE)
        entries = {
            line.split("==")[0].lower(): line.split("==")[1]
            for line in lock.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        for required in ("numpy", "pyyaml", "jsonschema", "pywin32"):
            self.assertIn(required, entries, f"{required} missing from the Windows lock")
        # transitive closure of jsonschema must be pinned too
        for dependency in ("attrs", "referencing", "rpds-py", "jsonschema-specifications"):
            self.assertIn(dependency, entries, f"{dependency} missing from the Windows lock")


class BundleBuilderTests(unittest.TestCase):
    def test_bundle_contains_version_source_and_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package = tmp_path / "src" / "description_pipeline"
            (package / "sources" / "solidworks").mkdir(parents=True)
            (package / "__init__.py").write_text("__version__ = '0.0.1'\n", encoding="utf-8")
            (package / "sources" / "__init__.py").write_text("", encoding="utf-8")
            (package / "sources" / "solidworks" / "__init__.py").write_text("", encoding="utf-8")
            out = tmp_path / "bundle.zip"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BUILD_BUNDLE),
                    "--source",
                    str(tmp_path / "src"),
                    "--out",
                    str(out),
                    "--version",
                    "0.0.1",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["version"], "0.0.1")
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                requirements = archive.read("requirements.txt").decode("utf-8")
                start_here = archive.read("START-HERE.txt").decode("utf-8")
            self.assertIn("version.json", names)
            self.assertIn("START-HERE.txt", names)
            self.assertIn("requirements.txt", names)
            self.assertIn("submit.ps1", names)
            self.assertIn("submit-host.example.json", names)
            self.assertIn("src/description_pipeline/sources/solidworks/__init__.py", names)
            self.assertIn("numpy==", requirements)
            self.assertIn("pywin32==", requirements)
            self.assertIn("docs/solidworks-first-use.en.md", start_here)
            # The paragraph above promises that the guided install runs the worker Doctor, and Setup
            # only reaches Doctor when it was given an assembly: the shown command must carry the
            # same flags as the guide, or the one command a newcomer copies is not the guided install.
            for flag in ("-Action Setup", "-Bundle", "-Assembly", "-AssemblyConfiguration"):
                self.assertIn(flag, start_here)
            self.assertIn(
                "-File .\\worker.ps1 -Action Setup -Bundle .\\description-worker-0.0.1-windows-x86_64.zip",
                start_here,
            )

    def test_default_version_is_the_locked_release(self) -> None:
        module = BUILD_BUNDLE.read_text(encoding="utf-8")
        self.assertIn("win-py312.lock", module)


if __name__ == "__main__":
    unittest.main()
