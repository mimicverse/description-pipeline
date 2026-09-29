"""``description doctor`` must tell a user what is wrong and how to fix it.

The command is the first thing a new installation runs, so the tests pin both directions: a healthy
installation and workspace pass, and every broken input produces a failure with an actionable fix
rather than a traceback.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline import cli, doctor
from description_pipeline.build import lock_toolchain
from description_pipeline.io import PipelineError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")
MODULES = list(doctor.REQUIRED) + list(doctor.OPTIONAL)


def workspace(temporary: str) -> Path:
    target = Path(temporary) / "demo-arm"
    shutil.copytree(EXAMPLE, target, ignore=IGNORED)
    lock_toolchain(target)
    return target


class EnvironmentCheckTests(unittest.TestCase):
    def workspace_with_a_worker(self, temporary: str, url: str = "http://127.0.0.1:8765") -> Path:
        """A SolidWorks workspace: the shipped examples use the fixture provider."""

        target = workspace(temporary)
        (target / "config/robot.yaml").write_text(
            json.dumps(
                {
                    "schema_version": "description.definition/v1",
                    "hardware_id": "demo-arm",
                    "overrides": [],
                    "source": {
                        "provider": "solidworks",
                        "assembly": "D:/robots/arm.SLDASM",
                        "configuration": "Default",
                        "worker_url": url,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    def test_a_reachable_worker_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.workspace_with_a_worker(temporary)
            pinned = doctor.tool_identity()["version"]
            with mock.patch.object(doctor, "worker_health", return_value={"worker_version": pinned}) as probe:
                report = doctor.run(target)
            check = next(item for item in report["checks"] if item["name"] == "worker")
            self.assertEqual(check["status"], doctor.OK)
            self.assertIn(pinned, check["detail"])
            self.assertEqual(probe.call_args.kwargs["timeout"], 5, "doctor must not wait for a long timeout")

    def test_a_worker_that_is_not_the_pinned_version_warns(self):
        """A stale worker captures with code the lock does not describe; say so before the capture."""

        with tempfile.TemporaryDirectory() as temporary:
            target = self.workspace_with_a_worker(temporary)
            pinned = doctor.tool_identity()["version"]
            with mock.patch.object(doctor, "worker_health", return_value={"worker_version": "0.3.13"}):
                report = doctor.run(target)
            check = next(item for item in report["checks"] if item["name"] == "worker")
            self.assertEqual(check["status"], doctor.WARN)
            self.assertIn("0.3.13", check["detail"])
            self.assertIn(pinned, check["detail"])
            self.assertIn("-Action Update", check["fix"])
            self.assertIn("tool lock", check["fix"])
            self.assertTrue(report["passed"], "a warning is not a failure")

    def test_an_unreachable_worker_warns_with_the_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = self.workspace_with_a_worker(temporary)
            with mock.patch.object(
                doctor,
                "worker_health",
                side_effect=PipelineError("Windows worker is not reachable at http://127.0.0.1:8765"),
            ):
                report = doctor.run(target)
            check = next(item for item in report["checks"] if item["name"] == "worker")
            self.assertEqual(check["status"], doctor.WARN)
            self.assertIn("worker.ps1 -Action Start", check["fix"])
            self.assertIn("--reuse-source", check["fix"])
            self.assertTrue(report["passed"], "a warning is not a failure")

    def test_a_fixture_workspace_has_no_worker_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = doctor.run(workspace(temporary))
        self.assertNotIn("worker", [item["name"] for item in report["checks"]])

    def test_a_long_windows_workspace_is_warned_about_before_it_fails(self):
        """Long paths are off by default, so this is a first-run condition, not an edge case."""

        with (
            mock.patch.object(doctor.platform, "system", return_value="Windows"),
            mock.patch.object(doctor, "_long_paths_enabled", return_value=False),
        ):
            check = doctor._path_length(Path("C:/robots/" + "d" * 170))
        self.assertEqual(check["status"], doctor.WARN)
        self.assertIn(str(doctor.MAX_PATH), check["detail"])
        self.assertIn("LongPathsEnabled", check["fix"])

    def test_a_short_windows_workspace_and_an_enabled_host_pass(self):
        with (
            mock.patch.object(doctor.platform, "system", return_value="Windows"),
            mock.patch.object(doctor, "_long_paths_enabled", return_value=False),
        ):
            short = doctor._path_length(Path("C:/robots/arm"))
        with (
            mock.patch.object(doctor.platform, "system", return_value="Windows"),
            mock.patch.object(doctor, "_long_paths_enabled", return_value=True),
        ):
            enabled = doctor._path_length(Path("C:/robots/" + "d" * 160))
        for check, label in ((short, "short root"), (enabled, "long paths enabled")):
            with self.subTest(case=label):
                self.assertEqual(check["status"], doctor.OK)
                self.assertNotIn("fix", check)

    def test_other_platforms_report_the_length_without_a_limit(self):
        with mock.patch.object(doctor.platform, "system", return_value="Linux"):
            check = doctor._path_length(Path("/srv/models/" + "d" * 160))
        self.assertEqual(check["status"], doctor.OK)
        self.assertIn("no MAX_PATH limit", check["detail"])

    def test_the_report_carries_the_path_length_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = doctor.run(workspace(temporary))
        check = next(item for item in report["checks"] if item["name"] == "path length")
        self.assertEqual(check["status"], doctor.OK)
        self.assertTrue(report["passed"], report["failed"])

    def test_a_low_disk_warns_with_the_fix(self):
        """``Disk quota exceeded`` during the first build is a worse introduction than a warning."""

        usage = shutil.disk_usage
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=usage("/")._replace(free=512 * 1024**2)):
            check = doctor._free_space(Path(tempfile.gettempdir()))
        self.assertEqual(check["status"], doctor.WARN)
        self.assertIn("2 GiB", check["detail"])
        self.assertIn("TMPDIR", check["fix"])

    def test_plenty_of_room_passes(self):
        check = doctor._free_space(Path(tempfile.gettempdir()))
        self.assertEqual(check["status"], doctor.OK)
        self.assertIn("GiB free", check["detail"])

    def test_the_report_carries_the_free_space_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = doctor.run(workspace(temporary))
        names = [item["name"] for item in report["checks"]]
        self.assertIn("disk space", names)

    def test_a_complete_environment_passes(self):
        present = dict.fromkeys(MODULES, "1.0")
        with (
            mock.patch.object(doctor, "_version", side_effect=present.get),
            mock.patch.object(doctor, "_external", return_value="tool 1.0"),
        ):
            report = doctor.run()
        self.assertTrue(report["passed"], report["failed"])
        self.assertEqual(report["warned"], [])

    def test_a_missing_required_package_fails_with_a_fix(self):
        present: dict[str, str | None] = dict.fromkeys(MODULES, "1.0")
        present["mujoco"] = None
        present["numpy"] = None
        with (
            mock.patch.object(doctor, "_version", side_effect=present.get),
            mock.patch.object(doctor, "_external", return_value="tool 1.0"),
        ):
            report = doctor.run()
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed"], ["packages"])
        self.assertEqual(report["warned"], ["optional packages"])
        packages = next(check for check in report["checks"] if check["name"] == "packages")
        self.assertIn("missing: numpy", packages["detail"])
        self.assertTrue(packages["fix"])

    def test_the_github_check_is_opt_in(self):
        present = dict.fromkeys(MODULES, "1.0")
        with (
            mock.patch.object(doctor, "_version", side_effect=present.get),
            mock.patch.object(doctor, "_external", return_value="tool 1.0"),
        ):
            without = doctor.run()
            with_github = doctor.run(github=True)
        self.assertNotIn("gh", [check["name"] for check in without["checks"]])
        self.assertIn("gh", [check["name"] for check in with_github["checks"]])

    def test_a_missing_external_tool_is_a_warning(self):
        present = dict.fromkeys(MODULES, "1.0")
        with (
            mock.patch.object(doctor, "_version", side_effect=present.get),
            mock.patch.object(doctor, "_external", return_value=None),
        ):
            report = doctor.run()
        self.assertTrue(report["passed"])
        self.assertEqual(sorted(report["warned"]), ["git", "git-lfs"])

    def test_a_missing_mujoco_hint_says_what_still_needs_it(self):
        """``check`` compiles the consumer scene on every profile, kinematics included."""

        present: dict[str, str | None] = dict.fromkeys(MODULES, "1.0")
        present["mujoco"] = None
        with (
            mock.patch.object(doctor, "_version", side_effect=present.get),
            mock.patch.object(doctor, "_external", return_value="tool 1.0"),
        ):
            report = doctor.run()
        check = next(item for item in report["checks"] if item["name"] == "optional packages")
        self.assertIn("simulation", check["fix"])
        self.assertIn("consumer", check["fix"])
        self.assertNotIn("needs none", check["fix"])


class WorkspaceCheckTests(unittest.TestCase):
    def test_a_relocked_workspace_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = doctor.run(workspace(temporary))
        self.assertTrue(report["passed"], report["failed"])
        names = [check["name"] for check in report["checks"]]
        self.assertIn("workspace", names)
        self.assertIn("toolchain lock", names)
        self.assertIn("source snapshot", names)
        self.assertIn("profiles", names)

    def test_a_foreign_lock_fails_with_the_relock_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = workspace(temporary)
            lock = target / "config/toolchain.lock.json"
            payload = json.loads(lock.read_text(encoding="utf-8"))
            payload["version"] = "0.0.1"
            lock.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
            report = doctor.run(target)
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed"], ["toolchain lock"])
        check = next(item for item in report["checks"] if item["name"] == "toolchain lock")
        self.assertIn("description tool lock", check["fix"])

    def test_missing_snapshot_profiles_and_manifest_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = workspace(temporary)
            (target / "sources" / "source.lock.json").unlink()
            shutil.rmtree(target / "config/profiles")
            (target / "manifest.json").unlink()
            report = doctor.run(target)
        self.assertIn("source snapshot", report["failed"])
        self.assertIn("profiles", report["failed"])
        self.assertIn("delivered bundle", report["warned"])

    def test_a_non_workspace_root_fails_quickly(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = doctor.run(Path(temporary))
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed"], ["workspace"])

    def test_lines_render_status_and_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            text = "\n".join(doctor.lines(doctor.run(Path(temporary))))
        self.assertIn("fail workspace", text)
        self.assertIn("→", text)


class BrokenLockTests(unittest.TestCase):
    """The doctor is what a user runs when something is wrong, so broken input is data, not a crash."""

    def report_with(self, temporary: str, relative: str, text: str | None) -> dict:
        target = workspace(temporary)
        path = target / relative
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
        return doctor.run(target)

    def check_named(self, report: dict, name: str) -> dict:
        return next(item for item in report["checks"] if item["name"] == name)

    def test_a_missing_toolchain_lock_is_a_check_with_a_fix(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.report_with(temporary, doctor.TOOLCHAIN_LOCK, None)
        check = self.check_named(report, "toolchain lock")
        self.assertEqual(check["status"], "fail")
        self.assertIn("missing", check["detail"])
        self.assertTrue(check["fix"])

    def test_a_broken_toolchain_lock_is_a_check_with_a_fix(self):
        # Unreadable JSON, JSON that is not an object, and duplicate keys all used to escape the
        # report - as a traceback or as an opaque top-level error that hides the other checks.
        for text in ("{", "[]", "null", '{"version": "0.3.14", "version": "0.3.13"}'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                report = self.report_with(temporary, doctor.TOOLCHAIN_LOCK, text)
                check = self.check_named(report, "toolchain lock")
                self.assertEqual(check["status"], "fail")
                self.assertTrue(check["fix"])
                self.assertIn("workspace", [item["name"] for item in report["checks"] if item["status"] == "ok"])

    def test_a_broken_source_lock_is_a_check_with_a_fix(self):
        for text in ("{", "[]", "null", '{"snapshot": 42}', '{"snapshot": ""}'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                report = self.report_with(temporary, doctor.SOURCE_LOCK, text)
                check = self.check_named(report, "source snapshot")
                self.assertEqual(check["status"], "fail")
                self.assertIn("source freeze", check["fix"])


class DoctorCliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_json_output_is_parseable_and_healthy(self):
        code, output = self.run_cli(["doctor", "--json"])
        payload = json.loads(output)
        self.assertEqual(payload["schema_version"], "description.doctor/v1")
        self.assertIn(payload["passed"], (True, False))
        self.assertNotIn("Traceback", output)
        self.assertEqual(code, 0 if payload["passed"] else 1)

    def test_human_output_names_every_check(self):
        code, output = self.run_cli(["doctor"])
        self.assertIn("python", output)
        self.assertIn("packages", output)
        self.assertEqual(code, 0 if "fail" not in output else 1)

    def test_a_broken_workspace_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, output = self.run_cli(["doctor", "--root", temporary])
        self.assertEqual(code, 1)
        self.assertIn("fail", output)

    def test_the_help_lists_the_common_task(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit) as exit_code:
            cli.main(["--help"])
        output = buffer.getvalue()
        self.assertEqual(exit_code.exception.code, 0)
        self.assertIn("description doctor", output)
        self.assertIn("description model update", output)


if __name__ == "__main__":
    unittest.main()
