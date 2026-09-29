import hashlib
import json
import os
import tempfile
import unittest

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import CadError, UsageError
from tools.solidworks_export.exporter import run_export
from tools.solidworks_export.model import RawComponent
from tools.solidworks_export.stl import validate_binary_stl
from tools.solidworks_export.transform import identity

from .helpers import make_config_dict


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-export-")
        self.out = os.path.join(self.tmp, "out")
        self.cfg = config_from_dict(make_config_dict())

    def test_full_export(self):
        stages = []
        result = run_export(
            FakeBackend(), self.cfg, self.out, "D:/models/FAKE.SLDASM", progress=lambda payload: stages.append(payload)
        )
        self.assertEqual(result["links"], 2)
        self.assertEqual(result["joints"], 1)
        self.assertTrue(result["shallow_check"]["ok"])
        self.assertGreater(result["shallow_check"]["checks"], 0)
        self.assertEqual(stages[0]["stage"], "collecting_scene")
        self.assertEqual(stages[-1], {"stage": "done", "percent": 100})
        percents = [entry["percent"] for entry in stages]
        self.assertEqual(percents, sorted(percents))
        self.assertIn("exporting_meshes", {entry["stage"] for entry in stages})
        self.assertTrue(os.path.isfile(os.path.join(self.out, "robot.urdf")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "manifest.sha256")))
        self.assertEqual(validate_binary_stl(os.path.join(self.out, "meshes/base_link.STL")), 12)
        self.assertEqual(validate_binary_stl(os.path.join(self.out, "meshes/arm_link.STL")), 12)

        with open(os.path.join(self.out, "robot.urdf"), encoding="utf-8") as handle:
            urdf = handle.read()
        self.assertIn('name="dof_arm"', urdf)
        self.assertIn('xyz="0 0 0.1"', urdf)
        self.assertIn('filename="meshes/base_link.STL"', urdf)

        with open(os.path.join(self.out, "cad_evidence.json"), encoding="utf-8") as handle:
            evidence = json.load(handle)
        self.assertEqual(evidence["evidence_class"], "synthetic")
        self.assertIn("environment", evidence)
        self.assertIn("api_contact", evidence)
        base = evidence["links"]["base_link"]
        arm = evidence["links"]["arm_link"]
        self.assertAlmostEqual(base["combined"]["mass"], 2.0, places=12)
        self.assertAlmostEqual(arm["combined"]["mass"], 1.0, places=12)
        self.assertAlmostEqual(base["combined"]["mass"] + arm["combined"]["mass"], 3.0, places=12)
        self.assertTrue(base["conditions"]["positive_definite"])
        self.assertIn("used_api", base["raw"]["Base-1"]["reference"])

        with open(os.path.join(self.out, "manifest.sha256"), encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            digest, rel = line.split("  ", 1)
            self.assertEqual(digest, _sha256(os.path.join(self.out, rel)), rel)

        with open(os.path.join(self.out, "native_source.json"), encoding="utf-8") as handle:
            native = json.load(handle)
        self.assertEqual(native["evidence_class"], "synthetic")
        self.assertEqual(native["source_kind"], "synthetic")
        self.assertEqual(native["schema_version"], "swbridge.native-source/v1")
        self.assertIn("python", native["environment"])
        self.assertIn("backend", native["api_contact"])
        self.assertEqual(native["urdf_sha256"], _sha256(os.path.join(self.out, "robot.urdf")))
        self.assertEqual(native["export_config_sha256"], _sha256(os.path.join(self.out, "export_config.json")))

        with open(os.path.join(self.out, "cad_inertia_evidence.json"), encoding="utf-8") as handle:
            sidecar = json.load(handle)
        self.assertEqual(sidecar["schema_version"], "solidworks-mass-properties/v1")
        self.assertEqual(sidecar["urdf_sha256"], native["urdf_sha256"])
        self.assertEqual(sidecar["source"]["kind"], "synthetic")
        self.assertFalse(sidecar["source"]["synthetic"] is False)
        self.assertEqual(sidecar["source"]["name"], "cad_evidence.json")
        self.assertEqual(sidecar["source"]["sha256"], _sha256(os.path.join(self.out, "cad_evidence.json")))
        self.assertEqual(set(sidecar["links"].keys()), {"base_link", "arm_link"})
        for record in sidecar["links"].values():
            self.assertFalse(record["frame_mapping_confirmed"])
            self.assertFalse(record["component_selection_confirmed"])
            self.assertEqual(set(record["L_at_com"].keys()), {"ixx", "iyy", "izz", "ixy", "ixz", "iyz"})

        # SolidWorks positive products (+0.002) must be written as the standard
        # negative product in the URDF/evidence, while the raw value is kept.
        arm_raw = arm["raw"]["Arm-1"]["inertia"]
        self.assertAlmostEqual(arm_raw[0][1], 0.002, places=12)
        self.assertEqual(arm["raw"]["Arm-1"]["product_convention"], "solidworks_positive")
        self.assertAlmostEqual(arm["combined"]["inertia"][0][1], -0.002, places=12)

    def test_non_empty_output_rejected(self):
        os.makedirs(self.out)
        with open(os.path.join(self.out, "keep.txt"), "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(UsageError):
            run_export(FakeBackend(), self.cfg, self.out, "D:/models/FAKE.SLDASM")

    def test_missing_component_rejected(self):
        data = make_config_dict()
        data["links"][1]["components"] = ["Nope-9"]
        cfg = config_from_dict(data)
        with self.assertRaises(Exception) as ctx:
            run_export(FakeBackend(), cfg, self.out, "D:/models/FAKE.SLDASM")
        self.assertFalse(os.path.exists(self.out))
        failed_dir = getattr(ctx.exception, "failed_dir", None)
        self.assertIsNotNone(failed_dir)
        assert failed_dir is not None
        self.assertTrue(os.path.isdir(failed_dir))
        self.assertTrue(os.path.isfile(os.path.join(failed_dir, "failure.log")))

    def test_duplicate_component_names_rejected(self):
        backend = FakeBackend()
        backend.components["Base-2"] = RawComponent(name="Base-1", transform=identity())
        with self.assertRaises(CadError) as ctx:
            run_export(backend, self.cfg, self.out, "D:/models/FAKE.SLDASM")
        self.assertEqual(ctx.exception.code, "cad_duplicate_component_name")


if __name__ == "__main__":
    unittest.main()
