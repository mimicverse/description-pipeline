"""按用途裁剪掉的规则，其例外不该反过来判死工作区。

运动学用途不评估 URDF407（"有 visual 但没有 collision"），而严格交付审计评估它；所以一个只做
运动学、视觉即形状记录的工作区，要过交付审计就得在 `config/urdf_quality.json` 里写下这条例外。
修复前这份合法例外会让 `description check` 报 `URDF702`"死例外"——同一份工作区，一个工具要求
写它，另一个工具因为它判不合格。现在"本次没有评估的编号"不算死例外，两个工具结论一致。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from description_pipeline.build import assess, build, freeze, lock_toolchain
from description_pipeline.sources.snapshot import write_manifest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")
WAIVER = {
    "waivers": [
        {
            "code": "URDF407",
            "reason": "kinematics-only workspace: the visuals are the shape of record",
            "owner": "tooling",
            "date": "2026-09-28",
        }
    ],
    "massless_links": [],
}


class PurposeScopedWaiverTests(unittest.TestCase):
    def prepare(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "demo-arm"
        shutil.copytree(EXAMPLE, root, ignore=IGNORED)
        scene_path = root / "sources/fixture/scene.json"
        scene = json.loads(scene_path.read_text(encoding="utf-8"))
        for link in scene["links"]:
            link["collisions"] = []
        scene_path.write_text(json.dumps(scene, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        write_manifest(
            root / "sources/fixture",
            kind="fixture",
            identity={"case": "kinematics-without-collisions"},
            evidence_class="fixture",
        )
        (root / "config/urdf_quality.json").write_text(
            json.dumps(WAIVER, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        lock_toolchain(root)
        freeze(root)
        return root

    def failed(self, report: dict) -> list[str]:
        return [item["id"] for item in report.get("checks", []) if item["status"] == "failed"]

    def test_a_kinematics_workspace_may_waive_the_collision_rule(self):
        root = self.prepare()
        built = build(root, "kinematics")
        self.assertTrue(built["passed"], built["blockers"])
        check = assess(root, "kinematics")
        self.assertTrue(check["passed"], check["blockers"])
        self.assertNotIn("URDF702", self.failed(check))

    def test_the_strict_audit_accepts_the_same_workspace(self):
        root = self.prepare()
        build(root, "kinematics")
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "audit.py"), "--root", str(root), "--policy", "strict"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
        self.assertNotIn("URDF702", result.stdout or "")
