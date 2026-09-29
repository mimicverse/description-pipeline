"""CAD without materials or coordinate systems: documented masses + explicit origins."""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import ConfigError
from tools.solidworks_export.exporter import run_export

from .helpers import make_config_dict


def element(parent: ET.Element, path: str) -> ET.Element:
    """取 XML 节点：缺失即断言失败（测试里比 None 继续更早暴露问题）。"""

    found = parent.find(path)
    assert found is not None, f"missing <{path}> under <{parent.tag}>"
    return found


def documented_config():
    data = make_config_dict()
    data["joints"][0].pop("coordinate_system")
    data["joints"][0]["origin"] = {"xyz": [0.0, 0.0, 0.1], "rpy": [0.0, 0.0, 0.0]}
    data["component_masses"] = {"Base-1": 0.25, "Arm-1": 0.125}
    data["mass_provenance"] = {
        "kind": "documented_source",
        "note": "printed parts at 15% infill, datasheet masses",
        "source": "authors' printing guide + BOM",
    }
    return data


class DocumentedMassConfigTests(unittest.TestCase):
    def test_masses_require_a_documented_provenance(self):
        data = make_config_dict()
        data["component_masses"] = {"Base-1": 0.25}
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_non_positive_mass_is_rejected(self):
        data = documented_config()
        data["component_masses"]["Base-1"] = 0.0
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_explicit_origin_replaces_the_missing_coordinate_system(self):
        cfg = config_from_dict(documented_config())
        joint = cfg.joints[0]
        self.assertIsNone(joint.coordinate_system)
        assert joint.origin is not None
        self.assertEqual(joint.origin["xyz"], [0.0, 0.0, 0.1])
        self.assertEqual(cfg.component_masses["Arm-1"], 0.125)

    def test_movable_joint_needs_an_origin_or_a_coordinate_system(self):
        data = make_config_dict()
        data["joints"][0].pop("coordinate_system")
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_origin_must_hold_three_numbers(self):
        data = documented_config()
        data["joints"][0]["origin"] = {"xyz": [0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}
        with self.assertRaises(ConfigError):
            config_from_dict(data)


class NameOverlapTests(unittest.TestCase):
    def test_link_and_joint_may_share_a_name(self):
        """URDF permits it, and the reference Microban model relies on it."""
        data = make_config_dict()
        data["joints"][0]["name"] = "base_link"
        cfg = config_from_dict(data)  # must not raise
        self.assertEqual(cfg.joints[0].name, "base_link")

    def test_two_joints_may_not_share_a_name(self):
        data = make_config_dict()
        data["joints"].append(dict(data["joints"][0]))
        with self.assertRaises(ConfigError):
            config_from_dict(data)


class FixedJointAxisTests(unittest.TestCase):
    def test_fixed_joint_may_declare_a_zero_axis(self):
        data = make_config_dict()
        data["links"].append({"name": "imu_link", "frame_component": "Base-1"})
        data["joints"].append(
            {
                "name": "imu_frame",
                "type": "fixed",
                "parent": "base_link",
                "child": "imu_link",
                "origin": {"xyz": [0, 0, 0.05], "rpy": [0, 0, 0]},
                "axis": [0.0, 0.0, 0.0],
            }
        )
        cfg = config_from_dict(data)
        self.assertIsNone(cfg.joints[-1].axis)

    def test_movable_joint_still_needs_a_real_axis(self):
        data = make_config_dict()
        data["joints"][0]["axis"] = [0.0, 0.0, 0.0]
        with self.assertRaises(ConfigError):
            config_from_dict(data)


class DocumentedMassExportTests(unittest.TestCase):
    def setUp(self):
        self.out = os.path.join(tempfile.mkdtemp(prefix="swbridge-docmass-"), "package")
        run_export(
            FakeBackend(), config_from_dict(documented_config()), self.out, "FAKE.SLDASM", evidence_class="synthetic"
        )
        with open(os.path.join(self.out, "robot.urdf"), encoding="utf-8") as handle:
            self.robot = ET.fromstring(handle.read())

    def test_urdf_uses_the_documented_masses(self):
        masses = {
            link.get("name"): float(element(link, "inertial/mass").get("value", "0"))
            for link in self.robot.findall("link")
        }
        self.assertAlmostEqual(masses["base_link"], 0.25, places=12)
        self.assertAlmostEqual(masses["arm_link"], 0.125, places=12)

    def test_explicit_origin_reaches_the_urdf_joint(self):
        joint = element(self.robot, "joint")
        origin = element(joint, "origin")
        self.assertEqual([float(v) for v in origin.get("xyz", "").split()], [0.0, 0.0, 0.1])
        self.assertEqual([float(v) for v in origin.get("rpy", "").split()], [0.0, 0.0, 0.0])

    def test_packaged_config_is_the_effective_config(self):
        """The copy inside the package must keep frames, origins and masses."""
        with open(os.path.join(self.out, "export_config.json"), encoding="utf-8") as handle:
            packaged = json.load(handle)
        self.assertEqual(packaged["mesh"]["path_prefix"], "meshes/")
        self.assertEqual(packaged["component_masses"]["Base-1"], 0.25)
        self.assertEqual(packaged["mass_provenance"]["kind"], "documented_source")
        self.assertEqual(packaged["joints"][0]["origin"]["xyz"], [0.0, 0.0, 0.1])

    def test_evidence_records_the_mass_source(self):
        with open(os.path.join(self.out, "cad_evidence.json"), encoding="utf-8") as handle:
            evidence = json.load(handle)
        self.assertEqual(evidence["mass_provenance"]["kind"], "documented_source")
        self.assertEqual(sorted(evidence["mass_provenance"]["documented_components"]), ["Arm-1", "Base-1"])
        base = evidence["links"]["base_link"]["raw"]["Base-1"]
        self.assertEqual(base["mass_source"], "documented")
        self.assertAlmostEqual(base["documented_mass"], 0.25, places=12)

    def test_inertia_scales_with_the_documented_mass(self):
        # Fake backend: Base-1 has 2 kg and ixx 0.1; a documented 0.25 kg must
        # scale the tensor by 1/8 instead of silently keeping the CAD value.
        with open(os.path.join(self.out, "robot.urdf"), encoding="utf-8") as handle:
            text = handle.read()
        link = element(ET.fromstring(text), "link")
        ixx = float(element(link, "inertial/inertia").get("ixx", "0"))
        self.assertAlmostEqual(ixx, 0.1 * 0.25 / 2.0, places=12)


if __name__ == "__main__":
    unittest.main()
