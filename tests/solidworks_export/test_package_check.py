import os
import tempfile
import unittest

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.exporter import run_export
from tools.solidworks_export.package_check import verify_package

from .helpers import make_config_dict


class PackageCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-check-")
        self.out = os.path.join(self.tmp, "out")
        cfg = config_from_dict(make_config_dict())
        run_export(FakeBackend(), cfg, self.out, "D:/models/FAKE.SLDASM")

    def test_clean_package_passes(self):
        report = verify_package(self.out)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["warnings"], [])

    def test_tampered_mesh_is_detected(self):
        mesh = os.path.join(self.out, "meshes", "base_link.STL")
        with open(mesh, "ab") as handle:
            handle.write(b"\x00")
        report = verify_package(self.out)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(item == "manifest_hash:meshes/base_link.STL" for item in report["errors"]), report["errors"]
        )

    def test_missing_required_file_is_detected(self):
        os.remove(os.path.join(self.out, "native_source.json"))
        report = verify_package(self.out)
        self.assertFalse(report["ok"])
        self.assertIn("required_file:native_source.json", report["errors"])
        self.assertIn("manifest_file_exists:native_source.json", report["errors"])

    def test_missing_package_is_detected(self):
        report = verify_package(os.path.join(self.tmp, "nope"))
        self.assertFalse(report["ok"])
        self.assertIn("package_exists", report["errors"])


if __name__ == "__main__":
    unittest.main()
