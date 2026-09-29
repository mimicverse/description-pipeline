"""质量入口（tools/quality.py）的回归：步骤编排与失败传播。"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("quality", Path(__file__).resolve().parents[1] / "tools/quality.py")
assert spec is not None and spec.loader is not None
quality = importlib.util.module_from_spec(spec)
sys.modules["quality"] = quality  # dataclass 需要模块已注册
spec.loader.exec_module(quality)


class QualityPlanTests(unittest.TestCase):
    def plan(self, command, **kwargs):
        # The POSIX plan everywhere: the Windows branch adds the two PowerShell suites, and these
        # expectations are about everything else.
        options = {"root": Path("/tmp/model"), "ruff": ["ruff"], "mypy": ["mypy"], "platform": "posix"}
        options.update(kwargs)
        return quality.plan(command, **options)

    def names(self, command, **kwargs):
        return [step.name for step in self.plan(command, **kwargs)]

    def test_lint_and_format_are_separate_commands(self):
        self.assertEqual(self.names("lint"), ["lint"])
        self.assertEqual(self.names("format"), ["format-check"])
        lint = self.plan("lint")[0]
        self.assertEqual(lint.command[:3], ["ruff", "check", "--config"])
        self.assertIn("tools", lint.command)

    def test_all_runs_lint_format_test_and_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            names = self.names("all", root=root)
        self.assertEqual(names[:3], ["lint", "format-check", "typecheck"])
        self.assertIn("test", names)
        self.assertIn("layout", names)
        self.assertIn("urdf-audit", names)
        self.assertNotIn("urdf-audit-report", names, "没有已提交报告时不该有报告门禁")

    def test_fast_skips_tests_but_keeps_model(self):
        names = self.names("all", fast=True)
        self.assertNotIn("test", names)
        self.assertIn("urdf-audit", names)

    def test_model_plan_includes_report_gate_when_report_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "docs" / "urdf_audit.json").write_text("{}", encoding="utf-8")
            names = self.names("model", root=root)
        self.assertEqual(names, ["layout", "urdf-audit", "urdf-audit-report"])

    def test_require_report_adds_the_gate_even_without_report(self):
        with tempfile.TemporaryDirectory() as folder:
            names = self.names("model", root=Path(folder), require_report=True)
        self.assertEqual(names, ["layout", "urdf-audit", "urdf-audit-report"])

    def test_mujoco_flag_is_forwarded(self):
        audit = next(step for step in self.plan("model", mujoco=True) if step.name == "urdf-audit")
        self.assertIn("--mujoco", audit.command)
        self.assertIn("--policy", audit.command)

    def test_missing_ruff_is_a_failure_unless_allowed(self):
        step = quality.Step("lint", [], tool="ruff")
        with mock.patch.object(quality, "tool_command", return_value=None), mock.patch.object(sys, "stdout"):
            self.assertEqual(quality.run([step], allow_missing_tools=False), 1)
            self.assertEqual(quality.run([step], allow_missing_tools=True), 0)

    def test_typecheck_uses_the_mypy_config(self):
        """Two passes: the second one reads the Windows stubs, so Linux sees Windows-only attributes."""

        self.assertEqual(self.names("typecheck"), ["typecheck", "typecheck-win32"])
        step = self.plan("typecheck")[0]
        self.assertEqual(step.command[:2], ["mypy", "--config-file"])
        self.assertEqual(step.tool, "mypy")
        win32 = self.plan("typecheck")[1]
        self.assertEqual(win32.command[:2], ["mypy", "--config-file"])
        self.assertEqual(win32.command[-2:], ["--platform", "win32"])
        self.assertEqual(win32.tool, "mypy")

    def test_the_windows_plan_runs_the_two_powershell_suites(self):
        """They only work there: their fixtures use `C:\\...` paths, and CI cannot run them any more."""

        with mock.patch.object(quality.shutil, "which", return_value=r"C:\Windows\System32\powershell.exe"):
            names = self.names("all", platform="nt")
            steps = {step.name: step for step in self.plan("all", platform="nt")}
        self.assertEqual(
            names[names.index("test") + 1 : names.index("test") + 3], ["powershell-deployment", "powershell-submit"]
        )
        self.assertEqual(steps["powershell-deployment"].command[1:4], ["-NoProfile", "-ExecutionPolicy", "Bypass"])
        self.assertTrue(steps["powershell-deployment"].command[-1].endswith("test_deployment.ps1"))
        self.assertTrue(steps["powershell-submit"].command[-1].endswith("test_submit.ps1"))
        self.assertNotIn("powershell-deployment", self.names("all", platform="posix"))

    def test_a_windows_plan_without_powershell_leaves_the_suites_out(self):
        with mock.patch.object(quality.shutil, "which", return_value=None):
            self.assertNotIn("powershell-deployment", self.names("all", platform="nt"))

    def test_missing_mypy_is_a_failure_unless_allowed(self):
        step = quality.Step("typecheck", [], tool="mypy")
        with mock.patch.object(quality, "tool_command", return_value=None), mock.patch.object(sys, "stdout"):
            self.assertEqual(quality.run([step], allow_missing_tools=False), 1)
            self.assertEqual(quality.run([step], allow_missing_tools=True), 0)

    def test_failing_step_propagates(self):
        step = quality.Step("boom", [sys.executable, "-c", "raise SystemExit(3)"])
        with mock.patch.object(sys, "stdout"):
            self.assertEqual(quality.run([step], allow_missing_tools=False), 1)

    def test_successful_step_passes(self):
        step = quality.Step("ok", [sys.executable, "-c", "print('fine')"])
        with mock.patch.object(sys, "stdout"):
            self.assertEqual(quality.run([step], allow_missing_tools=False), 0)

    def test_steps_run_in_utf8_mode(self):
        """A Chinese Windows box otherwise fails mypy on the comments in mypy.ini, not on types."""

        step = quality.Step("ok", [sys.executable, "-c", "print('fine')"])
        with (
            mock.patch.object(quality.subprocess, "run", return_value=mock.Mock(returncode=0)) as spawn,
            mock.patch.object(sys, "stdout"),
        ):
            self.assertEqual(quality.run([step], allow_missing_tools=False), 0)
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")


if __name__ == "__main__":
    unittest.main()
