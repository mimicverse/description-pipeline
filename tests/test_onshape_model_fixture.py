"""真实模型回归：对 Microban 导出结果跑工具链核验，锁住结构、质量与网格。

用例数据是 `tests/fixtures/onshape/cache/` 那份真实文档快照的导出结果，
仓库固定入口（`urdf/robot.urdf`、`mjcf/robot.xml`）在 main 上仍是空模板。
"""

import json
import sys
import unittest
from pathlib import Path
from typing import ClassVar

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import verify  # noqa: E402
from onshape_export import contacts  # noqa: E402
from tests import onshape_fixture  # noqa: E402

MODEL = Path(__file__).resolve().parent / "fixtures" / "onshape" / "model"

EXPECTED_LINKS = 24
EXPECTED_JOINTS = 23
EXPECTED_REVOLUTE = 19
EXPECTED_MASS_KG = 0.818978
EXPECTED_PRINTED_DENSITY = 957


@onshape_fixture.requires_fixture
class MicrobanModelFixtureTests(unittest.TestCase):
    report: ClassVar[dict]
    findings: ClassVar[dict]

    @classmethod
    def setUpClass(cls):
        cls.report = verify.verify(MODEL, use_mujoco=False)
        cls.findings = {finding["code"]: finding for finding in cls.report["findings"]}

    def test_structure_matches_the_source_document(self):
        structure = self.report["structure"]
        self.assertEqual(structure["links"], EXPECTED_LINKS)
        self.assertEqual(structure["joints"], EXPECTED_JOINTS)
        self.assertEqual(structure["revolute"], EXPECTED_REVOLUTE)
        self.assertEqual(structure["fixed"], EXPECTED_JOINTS - EXPECTED_REVOLUTE)

    def test_verification_has_no_errors(self):
        self.assertTrue(self.report["summary"]["passed"], self.report["findings"])
        self.assertEqual(self.report["summary"]["errors"], 0)

    def test_urdf_and_mjcf_agree(self):
        # OSV009（质量）与 OSV010（限位）不一致时会出现在 findings 里
        self.assertNotIn("OSV009", self.findings)
        self.assertNotIn("OSV010", self.findings)

    def test_mass_matches_the_category_density_budget(self):
        self.assertAlmostEqual(self.report["structure"]["total_mass_kg"], EXPECTED_MASS_KG, places=6)

    def test_meshes_are_referenced_canonically_and_present(self):
        links, _ = verify.parse_urdf(MODEL / "urdf" / "robot.urdf")
        references = {name for link in links.values() for name in link["meshes"]}
        self.assertEqual(len(references), 34)
        for reference in references:
            self.assertTrue(reference.startswith("../meshes/"), reference)
            self.assertTrue((MODEL / "urdf" / reference).is_file(), reference)

    def test_density_map_records_the_derivation(self):
        payload = json.loads((MODEL / "onshape" / "density_map.json").read_text(encoding="utf-8"))
        self.assertIn("derivation", payload["source"])
        self.assertEqual(payload["names"]["head__head"], EXPECTED_PRINTED_DENSITY)
        applied = json.loads((MODEL / "onshape" / "materials.json").read_text(encoding="utf-8"))
        self.assertEqual(len(applied["applied"]), 21)
        self.assertGreater(
            sum(row["mass_before_kg"] for row in applied["applied"]),
            sum(row["mass_after_kg"] for row in applied["applied"]),
        )

    def test_reference_comparison_is_recorded(self):
        reference = json.loads((MODEL / "onshape" / "verification.json").read_text(encoding="utf-8"))[
            "reference_comparison"
        ]
        self.assertEqual(reference["summary"]["joints_compared"], EXPECTED_JOINTS)
        self.assertEqual(reference["summary"]["range_mismatch"], 0)
        self.assertRegex(reference["reference"]["sha256"], r"^[0-9a-f]{64}$")

    def test_actuators_and_joint_dynamics_match_the_reference(self):
        """执行器应为力矩电机，关节阻尼/摩擦/转子惯量与官方 MJCF 对齐。"""

        mjcf = (MODEL / "mjcf" / "robot.xml").read_text(encoding="utf-8")
        self.assertEqual(mjcf.count("<motor "), EXPECTED_REVOLUTE)
        self.assertNotIn("<position class=", mjcf)
        for value in ('frictionloss="0.013"', 'armature="0.0018"', 'damping="0.041"'):
            self.assertEqual(mjcf.count(value), EXPECTED_REVOLUTE, value)
        findings = json.loads((MODEL / "onshape" / "verification.json").read_text(encoding="utf-8"))["findings"]
        codes = {finding["code"]: finding["severity"] for finding in findings}
        self.assertEqual(codes.get("OSV040"), "info")
        self.assertEqual(codes.get("OSV041"), "info")

    def test_contact_excludes_match_the_declared_input(self):
        """输入表、MJCF 与 manifest 三处的接触排除必须一致。"""

        declared = contacts.load(MODEL / "onshape" / "contact_excludes.json")
        mjcf = (MODEL / "mjcf" / "robot.xml").read_text(encoding="utf-8")
        self.assertEqual(contacts.declared(mjcf), declared)
        manifest = json.loads((MODEL / "onshape" / "layout.json").read_text(encoding="utf-8"))
        self.assertEqual([tuple(pair) for pair in manifest["contact_excludes"]], declared)
        self.assertEqual(len(manifest["meshes"]), 34)

    def test_contact_excludes_clear_the_zero_pose_self_contacts(self):
        """排除生效后零位应无自接触；报告里同时留有排除与自接触结论。"""

        self.assertEqual(self.findings["OSV036"]["severity"], "info")
        self.assertEqual(len(self.findings["OSV036"]["context"]["pairs"]), 4)
        recorded = {
            finding["code"]: finding
            for finding in json.loads((MODEL / "onshape" / "verification.json").read_text(encoding="utf-8"))["findings"]
        }
        self.assertIn("无自接触", recorded["OSV035"]["message"])
        self.assertEqual(recorded["OSV035"]["context"]["contacts"], 0)


if __name__ == "__main__":
    unittest.main()
