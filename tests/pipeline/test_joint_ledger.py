"""`config/joint_names.yaml` 是交付声明项，缺失、脱节或形状不对都必须判死。

`description check` 过去只按模型自身关节合成台账：一份和 URDF 脱节的清单可以拿到
`qualified_for`，完全没有台账的工作区也能通过——而交付布局与 `tools/audit.py --policy strict`
都要求这份文件（`URDF208`），六个 Microban 分支与 0.3.20 那次采集正是各差这一个文件。
现在两个工具同一条边界：带台账时按台账检查（缺登记、多登记、重复、形状不对都失败），
不带台账时 `URDF208` error，`description model init` 会生成带空清单的模板。
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from description_pipeline.build import assess, build, freeze, lock_toolchain
from tests.pipeline import ledger as joint_ledger

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")


def ledger(*names: str) -> str:
    return joint_ledger.text(*names)


class DeclaredJointLedgerTests(unittest.TestCase):
    def prepare(self, declared: str | None) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "demo-arm"
        shutil.copytree(EXAMPLE, root, ignore=IGNORED)
        lock_toolchain(root)
        path = root / "config/joint_names.yaml"
        if declared is None:
            path.unlink()
        else:
            path.write_text(declared, encoding="utf-8")
        freeze(root)
        return root

    def failed_codes(self, report: dict[str, Any]) -> list[str]:
        return [item["id"] for item in report.get("checks", []) if item["status"] == "failed"]

    def assert_refused(self, root: Path, report: dict[str, Any]) -> None:
        self.assertFalse(report["passed"], "a wrong ledger must not qualify")
        self.assertIn("URDF208", self.failed_codes(report))
        self.assertIn("URDF208", assess(root, "kinematics")["blockers"])

    def test_the_declared_ledger_qualifies(self):
        root = self.prepare(ledger("shoulder_joint", "elbow_joint"))
        report = build(root, "kinematics")
        self.assertTrue(report["passed"], report["blockers"])
        self.assertNotIn("URDF208", self.failed_codes(report))

    def test_a_ledger_that_missed_a_joint_is_refused(self):
        root = self.prepare(ledger("shoulder_joint"))
        self.assert_refused(root, build(root, "kinematics"))

    def test_a_ledger_naming_a_stranger_is_refused(self):
        root = self.prepare(ledger("shoulder_joint", "elbow_joint", "ghost_joint"))
        self.assert_refused(root, build(root, "kinematics"))

    def test_a_duplicate_entry_is_refused(self):
        root = self.prepare(ledger("shoulder_joint", "elbow_joint", "elbow_joint"))
        self.assert_refused(root, build(root, "kinematics"))

    def test_a_ledger_in_another_shape_is_refused(self):
        """A declaration that cannot be read must not fall back to the inferred ledger."""

        root = self.prepare("joints: [shoulder_joint, elbow_joint]\n")
        self.assert_refused(root, build(root, "kinematics"))

    def test_a_workspace_without_a_ledger_is_refused(self):
        """The delivered layout required the file; the pipeline refuses the same workspace now."""

        root = self.prepare(None)
        self.assert_refused(root, build(root, "kinematics"))
