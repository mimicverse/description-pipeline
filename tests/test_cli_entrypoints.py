"""入口脚本回归：四个入口按各自契约跑通（此前只有内部模块被测）。"""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests import onshape_fixture  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
FIXTURES = ROOT / "tests" / "fixtures" / "onshape"
MODEL = FIXTURES / "model"


def load_entry(name: str):
    """按文件加载入口脚本，并注册到 sys.modules（dataclass 需要）。"""

    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"entry_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"entry_{name}"] = module
    spec.loader.exec_module(module)
    return module


def run_main(module, argv) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = module.main(argv)
    return code, buffer.getvalue()


def model_workspace(root: Path) -> Path:
    """把 fixture 的资产复制成一个完整模型工作区（check_layout 要求的资产集）。"""

    for name in ("urdf", "meshes", "mjcf", "config", "onshape"):
        shutil.copytree(MODEL / name, root / name)
    (root / "config" / "model_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "mimicverse.description/v1",
                "model_id": "robot",
                "state": "candidate",
                "evidence": ["onshape"],
            }
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    (root / "docs" / "provenance").mkdir(parents=True)
    (root / "docs" / "quality.md").write_text("# 质量状态\n", encoding="utf-8")
    (root / "docs" / "quality.json").write_text('{"state": "candidate"}', encoding="utf-8")
    (root / "docs" / "provenance" / "README.md").write_text("# 来源\n", encoding="utf-8")
    return root


class EntryPointTests(unittest.TestCase):
    @onshape_fixture.requires_fixture
    def test_audit_entry_reports_json_for_a_real_model(self):
        audit = load_entry("audit")
        code, output = run_main(audit, ["--root", str(MODEL), "--json"])
        self.assertEqual(code, 0, output)
        self.assertTrue(output.lstrip().startswith("{"), repr(output))
        report = json.loads(output)
        self.assertTrue(report["passed"])
        self.assertEqual(report["model"]["links"], 24)

    def test_audit_entry_fails_on_a_broken_model(self):
        audit = load_entry("audit")
        with tempfile.TemporaryDirectory() as folder:
            code, _output = run_main(audit, ["--root", folder, "--json"])
        self.assertEqual(code, 2, "缺 URDF 时应当是用法错误（退出码 2）")

    @onshape_fixture.requires_fixture
    def test_onshape_entry_check_runs_offline_on_the_snapshot(self):
        entry = load_entry("onshape_to_urdf")
        code, output = run_main(
            entry,
            ["check", "--cache", str(FIXTURES / "cache"), "--offline", "--json"],
        )
        self.assertEqual(code, 0, output)
        report = json.loads(output)
        self.assertEqual(report["command"], "check")
        self.assertTrue(report["summary"]["passed"])

    @onshape_fixture.requires_fixture
    def test_onshape_entry_verify_runs_on_the_fixture_model(self):
        entry = load_entry("onshape_to_urdf")
        code, output = run_main(entry, ["verify", "--out", str(MODEL), "--json"])
        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)["summary"]["passed"])

    def test_quality_entry_lint_fails_without_ruff_but_can_be_allowed(self):
        quality = load_entry("quality")
        with mock.patch.object(quality, "ruff_command", return_value=None):
            allowed, _ = run_main(quality, ["lint", "--allow-missing-tools"])
            strict, _ = run_main(quality, ["lint"])
        self.assertEqual(allowed, 0)
        self.assertEqual(strict, 1)

    @onshape_fixture.requires_fixture
    def test_quality_entry_model_runs_layout_and_audit(self):
        quality = load_entry("quality")
        with tempfile.TemporaryDirectory() as folder:
            workspace = model_workspace(Path(folder))
            code, output = run_main(quality, ["model", "--root", str(workspace)])
        self.assertEqual(code, 0, output)
        self.assertIn("urdf-audit", output)


if __name__ == "__main__":
    unittest.main()
