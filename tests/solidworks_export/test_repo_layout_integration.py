"""An export can be dropped into a feature-branch layout of this repository."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.check_layout import check as check_layout
from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import ConfigError
from tools.solidworks_export.exporter import run_export

from .helpers import make_config_dict

REPO = Path(__file__).resolve().parents[2]


def _repo_contract(state):
    return {"model_id": "robot", "state": state}


class MeshPathPrefixTests(unittest.TestCase):
    def test_default_prefix_keeps_the_package_layout(self):
        cfg = config_from_dict(make_config_dict())
        self.assertEqual(cfg.mesh_path_prefix, "meshes/")

    def test_repository_prefix_is_accepted(self):
        data = make_config_dict()
        data.setdefault("mesh", {})["path_prefix"] = "../meshes/"
        self.assertEqual(config_from_dict(data).mesh_path_prefix, "../meshes/")

    def test_invalid_prefixes_are_rejected(self):
        for bad in ("/abs/meshes/", "http://host/meshes/", "meshes", "..\\meshes\\", "a/b/meshes/"):
            data = make_config_dict()
            data.setdefault("mesh", {})["path_prefix"] = bad
            with self.assertRaises(ConfigError, msg=bad):
                config_from_dict(data)


class RepoLayoutIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swbridge-repo-layout-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 模型分支只放资产：工具、测试与 CI 都留在 main，不复制到分支工作区
        for name in ("README.md", "urdf", "meshes", "mjcf", "config"):
            source = REPO / name if name == "README.md" else REPO / "tests/fixtures/legacy-template" / name
            if source.is_dir():
                shutil.copytree(source, self.tmp / name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, self.tmp / name)
        (self.tmp / "docs").mkdir(exist_ok=True)
        for name in ("quality.md", "quality.json"):
            shutil.copy2(REPO / "tests/fixtures/legacy-template/docs" / name, self.tmp / "docs" / name)
        shutil.copytree(REPO / "tests/fixtures/legacy-template/docs/provenance", self.tmp / "docs" / "provenance")
        (self.tmp / "config" / "model_contract.json").write_text(
            json.dumps(_repo_contract("candidate")), encoding="utf-8"
        )
        (self.tmp / "mjcf" / "robot.xml").write_text(
            '<mujoco model="robot"><worldbody><body name="base_link"/></worldbody></mujoco>', encoding="utf-8"
        )

    def test_export_with_repository_prefix_passes_the_layout_check(self):
        data = make_config_dict()
        data["model"] = "robot"
        data["mesh"] = {"format": "stl_binary", "merge": "per_link", "path_prefix": "../meshes/"}
        data["links"].append({"name": "imu_link", "frame_component": "Base-1"})
        data["joints"].append({"name": "imu_frame", "type": "fixed", "parent": "base_link", "child": "imu_link"})
        out = Path(tempfile.mkdtemp(prefix="swbridge-export-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        run_export(FakeBackend(), config_from_dict(data), str(out), "FAKE.SLDASM", evidence_class="synthetic")
        shutil.copy2(out / "robot.urdf", self.tmp / "urdf" / "robot.urdf")
        for mesh in (out / "meshes").glob("*.STL"):
            shutil.copy2(mesh, self.tmp / "meshes" / mesh.name)
        report = check_layout(self.tmp, role="model")
        self.assertTrue(report["layout_passed"])
        urdf = (self.tmp / "urdf" / "robot.urdf").read_text(encoding="utf-8")
        self.assertIn('filename="../meshes/base_link.STL"', urdf)


if __name__ == "__main__":
    unittest.main()
