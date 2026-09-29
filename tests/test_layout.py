"""仓库布局门禁的回归：main 必须有工具、模型分支只放资产与证据。"""

import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_layout", Path(__file__).resolve().parents[1] / "tools/check_layout.py"
)
assert spec is not None and spec.loader is not None
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


def _stl() -> bytes:
    out = bytearray(b"\0" * 80 + struct.pack("<I", 1))
    out += struct.pack("<3f", 0.0, 0.0, 1.0)
    for vertex in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        out += struct.pack("<3f", *vertex)
    return bytes(out + b"\0\0")


def write_model_root(
    root: Path, *, evidence=(), model_id="robot", state="candidate", scene_include="robot.xml", extra_lines=()
) -> Path:
    """造一个最小但完整的模型分支工作区（资产 + 契约 + 质量记录 + 证据）。"""

    for folder in ("urdf", "meshes", "mjcf", "config", "docs/provenance"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# 模型\n", encoding="utf-8")
    (root / "meshes/part.stl").write_bytes(_stl())
    (root / "urdf/robot.urdf").write_text(
        '<?xml version="1.0"?>\n<robot name="robot">\n'
        '  <link name="base_link"><visual><geometry>'
        '<mesh filename="../meshes/part.stl"/></geometry></visual></link>\n'
        '  <link name="tip_link"/>\n'
        '  <joint name="tip_joint" type="revolute">\n'
        '    <parent link="base_link"/><child link="tip_link"/>\n'
        '    <origin xyz="0 0 0.1" rpy="0 0 0"/><axis xyz="0 0 1"/>\n'
        '    <limit lower="-1" upper="1" effort="1" velocity="1"/>\n'
        "  </joint>\n</robot>\n",
        encoding="utf-8",
    )
    (root / "mjcf/robot.xml").write_text(
        '<mujoco model="robot"><worldbody><body name="base_link">'
        '<inertial pos="0 0 0" mass="1" diaginertia="0.001 0.001 0.001"/>'
        "</body></worldbody></mujoco>\n",
        encoding="utf-8",
    )
    (root / "mjcf/scene.xml").write_text(
        f'<mujoco model="scene"><include file="{scene_include}"/></mujoco>\n', encoding="utf-8"
    )
    contract = {"schema_version": "mimicverse.description/v1", "model_id": model_id, "state": state}
    if evidence:
        contract["evidence"] = list(evidence)
    (root / "config/model_contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (root / "config/joint_names.yaml").write_text("structural_joint_names:\n  - tip_joint\n", encoding="utf-8")
    (root / "config/urdf_quality.json").write_text('{"waivers": [], "massless_links": []}', encoding="utf-8")
    (root / "docs/quality.md").write_text("# 质量状态\n", encoding="utf-8")
    (root / "docs/quality.json").write_text('{"state": "candidate"}', encoding="utf-8")
    (root / "docs/provenance/README.md").write_text("# 来源凭证\n", encoding="utf-8")
    for line in extra_lines:
        target = root / line
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture", encoding="utf-8")
    return root


class MainLayoutTests(unittest.TestCase):
    def test_main_repo_layout_passes(self):
        result = layout.check()
        self.assertTrue(result["layout_passed"])
        self.assertEqual(result["role"], "main")
        self.assertFalse(result["candidate"])

    def test_missing_entry_fails(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            layout.check(Path(folder))

    def test_main_rejects_model_and_reports_tooling_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pyproject.toml").write_text("[project]\n")
            (root / "urdf").mkdir()
            with self.assertRaisesRegex(ValueError, "hardware-free"):
                layout.check(root, branch="main")

    def test_model_branch_rejects_empty_template(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), state="template")
            with self.assertRaisesRegex(ValueError, "empty template"):
                layout.check(root, role="model")

    def test_ros_build_and_display_entries_rejected(self):
        for relative in (
            "package.xml",
            "CMakeLists.txt",
            "config/display.rviz",
            "launch/display.launch",
        ):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                entry = root / relative
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_text("test fixture")
                with self.assertRaisesRegex(ValueError, "forbidden"):
                    layout.check_ros_free(root)

    def test_ros_requirements_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tools").mkdir()
            for name in layout.ROS_DISTRIBUTIONS:
                (root / "tools/requirements.txt").write_text(name + "==1.0\n")
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "ROS dependency"),
                ):
                    layout.check_ros_free(root)

    def test_ros_imports_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tools").mkdir()
            for code in (
                "import rospy",
                "from catkin_pkg.package import parse_package",
                "from urdf_parser_py.urdf import URDF",
            ):
                (root / "tools/example.py").write_text(code)
                with (
                    self.subTest(code=code),
                    self.assertRaisesRegex(ValueError, "ROS import"),
                ):
                    layout.check_ros_free(root)

    def test_original_provenance_is_not_an_operational_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "docs/provenance/original"
            original.mkdir(parents=True)
            (original / "package.xml").write_text("immutable evidence")
            layout.check_ros_free(root)

    def test_only_relative_mesh_paths_are_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "urdf").mkdir()
            (root / "meshes").mkdir()
            mesh = root / "meshes/part.stl"
            mesh.write_bytes(b"existence-only fixture, not physics validation")
            self.assertEqual(layout.resolve_mesh(root, "../meshes/part.stl"), mesh)
            for uri in (
                "package://robot_description/meshes/part.stl",
                str(mesh),
                "https://example.invalid/part.stl",
            ):
                with (
                    self.subTest(uri=uri),
                    self.assertRaisesRegex(ValueError, "Noncanonical"),
                ):
                    layout.resolve_mesh(root, uri)
            for uri in ("../meshes/absent.stl", "../meshes/../../outside.stl"):
                with (
                    self.subTest(uri=uri),
                    self.assertRaisesRegex(ValueError, "Missing or escaped"),
                ):
                    layout.resolve_mesh(root, uri)


class ModelBranchLayoutTests(unittest.TestCase):
    """工具只放 main：模型分支只接受资产与 contract.evidence 声明的证据目录。"""

    def test_asset_only_model_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), evidence=("onshape",))
            (root / "onshape").mkdir()
            result = layout.check(root)
            self.assertEqual(result["role"], "model")
            self.assertTrue(result["candidate"])

    def test_tools_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), extra_lines=("tools/audit.py",))
            with self.assertRaisesRegex(ValueError, "只放 main"):
                layout.check(root)

    def test_tests_and_ci_are_rejected(self):
        for name in ("tests/test_layout.py", ".github/workflows/validate.yml"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = write_model_root(Path(folder), extra_lines=(name,))
                with self.assertRaisesRegex(ValueError, "只放 main"):
                    layout.check(root)

    def test_main_only_docs_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), extra_lines=("docs/urdf_standard.md",))
            with self.assertRaisesRegex(ValueError, "只放 main"):
                layout.check(root)

    def test_main_only_dotfiles_are_rejected(self):
        for name in (".editorconfig", ".pre-commit-config.yaml"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = write_model_root(Path(folder))
                (root / name).write_text("fixture", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "只放 main"):
                    layout.check(root)

    def test_undeclared_top_level_entry_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), extra_lines=("scripts/helper.py",))
            with self.assertRaisesRegex(ValueError, "未声明的顶层条目"):
                layout.check(root)

    def test_declared_evidence_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), evidence=("onshape", "solidworks"))
            (root / "onshape").mkdir()
            (root / "solidworks").mkdir()
            self.assertTrue(layout.check(root)["layout_passed"])

    def test_model_id_must_be_robot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), model_id="robot_revision")
            with self.assertRaisesRegex(ValueError, "Model ID"):
                layout.check(root)

    def test_scene_must_include_robot_xml(self):
        with tempfile.TemporaryDirectory() as folder:
            root = write_model_root(Path(folder), scene_include="other.xml")
            with self.assertRaisesRegex(ValueError, "common robot.xml"):
                layout.check(root)


if __name__ == "__main__":
    unittest.main()
