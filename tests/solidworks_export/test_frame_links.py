"""Massless sensor/foot frames: kinematics only, no invented mass properties."""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import ConfigError
from tools.solidworks_export.exporter import run_export
from tools.solidworks_export.package_check import verify_package

from .helpers import make_config_dict


def frame_config():
    data = make_config_dict()
    data["mesh"] = {"format": "stl_binary", "merge": "per_link", "path_prefix": "../meshes/"}
    data["links"].append({"name": "imu_link", "frame_component": "Base-1"})
    data["joints"].append({"name": "imu_frame", "type": "fixed", "parent": "base_link", "child": "imu_link"})
    return data


class FrameConfigTests(unittest.TestCase):
    def test_frame_link_needs_a_frame_component(self):
        data = make_config_dict()
        data["links"].append({"name": "imu_link"})
        with self.assertRaises(ConfigError):
            config_from_dict(data)

    def test_frame_link_is_marked_and_has_no_components(self):
        cfg = config_from_dict(frame_config())
        frame = cfg.links[-1]
        self.assertTrue(frame.is_frame)
        self.assertEqual(frame.components, [])
        self.assertEqual(frame.frame_component, "Base-1")

    def test_solid_links_still_require_components(self):
        data = make_config_dict()
        data["links"][0]["components"] = []
        with self.assertRaises(ConfigError):
            config_from_dict(data)


class FrameExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-frames-")
        self.package = os.path.join(self.tmp, "package")

    def export(self):
        run_export(
            FakeBackend(), config_from_dict(frame_config()), self.package, "FAKE.SLDASM", evidence_class="synthetic"
        )
        return ET.parse(os.path.join(self.package, "robot.urdf")).getroot()

    def test_urdf_frame_link_is_empty_and_joint_is_fixed(self):
        robot = self.export()
        links = {link.get("name"): link for link in robot.findall("link")}
        self.assertIn("imu_link", links)
        frame = links["imu_link"]
        self.assertIsNone(frame.find("inertial"))
        self.assertIsNone(frame.find("visual"))
        self.assertIsNone(frame.find("collision"))
        joints = {joint.get("name"): joint for joint in robot.findall("joint")}
        self.assertEqual(joints["imu_frame"].get("type"), "fixed")
        self.assertIsNone(joints["imu_frame"].find("axis"))
        self.assertIsNone(joints["imu_frame"].find("limit"))

    def test_no_mesh_or_mass_is_claimed_for_a_frame(self):
        self.export()
        self.assertEqual(sorted(os.listdir(os.path.join(self.package, "meshes"))), ["arm_link.STL", "base_link.STL"])
        with open(os.path.join(self.package, "cad_evidence.json"), encoding="utf-8") as handle:
            evidence = json.load(handle)
        entry = evidence["links"]["imu_link"]
        self.assertEqual(entry["kind"], "frame")
        self.assertEqual(entry["mass_properties"], "not_applicable")
        with open(os.path.join(self.package, "cad_inertia_evidence.json"), encoding="utf-8") as handle:
            sidecar = json.load(handle)
        self.assertEqual(sidecar["links"]["imu_link"]["kind"], "frame")
        self.assertEqual(sidecar["links"]["imu_link"]["product_convention"], "not_applicable")

    def test_package_with_a_frame_still_verifies(self):
        self.export()
        report = verify_package(self.package)
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(any(check["check"].startswith("evidence_frame_has_no_inertial") for check in report["checks"]))


if __name__ == "__main__":
    unittest.main()
