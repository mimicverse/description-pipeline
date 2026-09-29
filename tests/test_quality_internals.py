"""质检内部件回归：例外台账的格式错误、布局检查的 CLI 入口。"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from urdf_quality import waivers  # noqa: E402
import quality  # noqa: E402


class QualityEnvironmentTests(unittest.TestCase):
    """The gate names an unusable environment instead of letting a test fail obscurely."""

    def test_tmpdir_problems_name_the_real_cause(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(quality.tmpdir_problems({"TMPDIR": folder}), [])
            self.assertEqual(quality.tmpdir_problems({}), [])
            relative = quality.tmpdir_problems({"TMPDIR": "relative-tmp"})
            self.assertEqual(len(relative), 1)
            self.assertIn("absolute", relative[0])
            missing = quality.tmpdir_problems({"TMPDIR": str(Path(folder) / "missing")})
            self.assertEqual(len(missing), 1)
            self.assertIn("does not exist", missing[0])

    def test_the_test_step_refuses_a_broken_tmpdir_before_running_it(self):
        step = quality.Step("test", [sys.executable, "-c", "raise SystemExit(1)"])
        with (
            mock.patch.dict(os.environ, {"TMPDIR": "relative-tmp"}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(quality.run([step], allow_missing_tools=False), 1)
        printed = output.getvalue()
        self.assertIn("TMPDIR is relative", printed)
        self.assertIn("未通过: 0/1 步", printed)


class WaiverLedgerTests(unittest.TestCase):
    def _load(self, payload) -> waivers.Ledger:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "urdf_quality.json"
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
            return waivers.load(path)

    def test_missing_file_is_an_empty_ledger(self):
        ledger = waivers.load(Path("/nonexistent/urdf_quality.json"))
        self.assertEqual(ledger.waivers, [])
        self.assertEqual(ledger.errors, [])

    def test_broken_json_is_reported_not_raised(self):
        ledger = self._load("{not json")
        self.assertEqual(len(ledger.errors), 1)
        self.assertIn("不是合法 JSON", ledger.errors[0])

    def test_non_object_and_bad_field_shapes_are_reported(self):
        cases = {
            "non-object": ([], "顶层必须是对象"),
            "waivers-not-list": ({"waivers": "x"}, "waivers 必须是数组"),
            "entry-not-object": ({"waivers": ["x"]}, "不是对象"),
            "missing-fields": ({"waivers": [{"code": "URDF308"}]}, "缺少字段"),
            "bad-code": (
                {"waivers": [{"code": "OSV001", "reason": "r", "owner": "o", "date": "2026-01-01"}]},
                "编号不合法",
            ),
            "bad-date": (
                {"waivers": [{"code": "URDF308", "reason": "r", "owner": "o", "date": "yesterday"}]},
                "ISO",
            ),
            "massless-not-list": ({"massless_links": "x"}, "字符串数组"),
        }
        for name, (payload, fragment) in cases.items():
            with self.subTest(name=name):
                ledger = self._load(payload)
                self.assertTrue(ledger.errors, name)
                self.assertTrue(any(fragment in error for error in ledger.errors), ledger.errors)

    def test_waiver_matches_code_and_optional_subject(self):
        ledger = self._load(
            {
                "waivers": [
                    {"code": "URDF308", "reason": "r", "owner": "o", "date": "2026-01-01"},
                    {
                        "code": "URDF309",
                        "subject": "imu_link",
                        "reason": "r",
                        "owner": "o",
                        "date": "2026-01-01",
                        "review_after": "2027-01-01",
                    },
                ],
                "massless_links": ["imu_link"],
            }
        )
        self.assertEqual(ledger.massless_links, {"imu_link"})
        from urdf_quality.findings import ERROR, WARNING, Finding

        self.assertIsNotNone(ledger.match(Finding("URDF308", WARNING, "x", "any")))
        self.assertIsNotNone(ledger.match(Finding("URDF309", WARNING, "x", "imu_link")))
        self.assertIsNone(ledger.match(Finding("URDF309", WARNING, "x", "other_link")))
        self.assertIsNone(ledger.match(Finding("URDF310", ERROR, "x", "any")))


class CheckLayoutEntryTests(unittest.TestCase):
    """直接跑真实 CLI（子进程），覆盖 __main__ 分支与 --github-output 契约。"""

    def _module(self):
        spec = importlib.util.spec_from_file_location("check_layout_main", TOOLS / "check_layout.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["check_layout_main"] = module
        spec.loader.exec_module(module)
        return module

    def test_main_returns_zero_and_writes_candidate(self):
        layout = self._module()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nested" / "github_output.txt"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = layout.main(["--root", str(ROOT), "--github-output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["role"], "main")
            self.assertEqual(output.read_text(encoding="utf-8"), "candidate=false\n")

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOLS / "check_layout.py"), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_cli_writes_the_github_output_and_prints_json(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "github_output.txt"
            result = self._run("--root", str(ROOT), "--github-output", str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "candidate=false\n")
            self.assertEqual(json.loads(result.stdout)["role"], "main")

    def test_cli_rejects_a_wrong_role_for_the_template(self):
        result = self._run("--root", str(ROOT), "--role", "model")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Role mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
