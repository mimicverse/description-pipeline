"""Shipped example export configs must always parse with the real loader."""

import json
import unittest
from pathlib import Path

from tools.solidworks_export.config import config_from_dict

EXAMPLES = Path(__file__).resolve().parents[2] / "tools" / "solidworks_export" / "examples"


def _load(name):
    with open(EXAMPLES / name, encoding="utf-8") as handle:
        return json.load(handle)


class ExampleFilesTests(unittest.TestCase):
    def test_export_config_example(self):
        cfg = config_from_dict(_load("export_config.example.json"))
        self.assertEqual(cfg.model, "fake_robot_v1")

    def test_shipped_synthetic_package_verifies(self):
        """The committed example package must pass the same check users run."""
        import json as _json

        from tools.solidworks_export.package_check import verify_package

        report = verify_package(str(EXAMPLES / "synthetic-export"))
        self.assertTrue(report["ok"], report["errors"])
        sidecar = _json.loads((EXAMPLES / "synthetic-export" / "cad_inertia_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["generator"]["tool"], "solidworks_export")


if __name__ == "__main__":
    unittest.main()
