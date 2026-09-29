"""``source_length_unit='mm'`` must still produce an SI package.

The fake host reports everything in millimetres (transforms, COM, inertia in
kg*mm^2, mesh vertices in mm); the exporter has to deliver metres / kg / kg*m^2
in the URDF, the evidence and the merged mesh.
"""

import json
import os
import tempfile
import unittest

from tools.solidworks_export.backends import Backend, _write_box_stl
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import CadError
from tools.solidworks_export.exporter import run_export
from tools.solidworks_export.model import RawComponent, RawScene
from tools.solidworks_export.stl import aabb, signed_volume
from tools.solidworks_export.transform import from_xyz_rpy


class MillimetreBackend(Backend):
    name = "fake"

    def __init__(self):
        self.components = {
            "Box-1": RawComponent(name="Box-1", transform=from_xyz_rpy((100.0, 0.0, 0.0), (0.0, 0.0, 0.0))),
        }
        self.mass_properties = {
            "Box-1": {
                "mass": 6.0,
                "com": (50.0, 0.0, 0.0),
                "inertia": ((10000.0, 0.0, 0.0), (0.0, 10000.0, 0.0), (0.0, 0.0, 10000.0)),
                "reference": {"used_api": "fixture-mm"},
            },
        }

    def health(self):
        return {"ok": True, "backend": "fake", "sw_version": "fixture-mm"}

    def collect_scene(self, doc_path, coordinate_systems, progress=None, require_material=True):
        return RawScene(
            document=doc_path,
            components=list(self.components.values()),
            coordinate_systems={},
            mass_properties=dict(self.mass_properties),
            notes={"backend": "fixture-mm"},
        )

    def export_component_mesh(self, component, dest_path, progress=None):
        if component != "Box-1":
            raise CadError("cad_missing_component", f"unknown {component!r}")
        # 100 mm cube, i.e. document-unit vertices
        _write_box_stl(dest_path, (100.0, 100.0, 100.0), (0.0, 0.0, 0.0))
        return {"component": component, "written": dest_path, "used_api": "fixture-mm", "triangles": 12}


def _mm_config():
    return config_from_dict(
        {
            "schema_version": "swbridge.export-config/v1",
            "model": "mm_fixture",
            "source_length_unit": "mm",
            "mesh": {"format": "stl_binary", "merge": "per_link"},
            "links": [{"name": "box_link", "components": ["Box-1"]}],
            "joints": [],
        }
    )


class MillimetreUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-mm-")
        self.out = os.path.join(self.tmp, "out")
        run_export(MillimetreBackend(), _mm_config(), self.out, "D:/models/MM.SLDASM")

    def test_urdf_is_si(self):
        with open(os.path.join(self.out, "robot.urdf"), encoding="utf-8") as handle:
            urdf = handle.read()
        self.assertIn('xyz="0.05 0 0"', urdf)  # COM in metres
        self.assertIn('ixx="0.01"', urdf)  # kg*m^2
        self.assertIn('mass value="6"', urdf)

    def test_evidence_keeps_raw_and_scaled_values(self):
        with open(os.path.join(self.out, "cad_evidence.json"), encoding="utf-8") as handle:
            evidence = json.load(handle)
        link = evidence["links"]["box_link"]
        self.assertEqual(link["source_length_unit"], "mm")
        self.assertEqual(link["raw"]["Box-1"]["com"], [50.0, 0.0, 0.0])
        self.assertEqual(link["raw"]["Box-1"]["inertia"][0][0], 10000.0)
        self.assertAlmostEqual(link["combined"]["com"][0], 0.05, places=12)
        self.assertAlmostEqual(link["combined"]["inertia"][0][0], 0.01, places=12)

    def test_mesh_is_scaled_to_metres(self):
        mesh = os.path.join(self.out, "meshes", "box_link.STL")
        mins, maxs = aabb(mesh)
        for axis in range(3):
            self.assertAlmostEqual(maxs[axis] - mins[axis], 0.1, places=6)
        self.assertAlmostEqual(signed_volume(mesh), 0.001, places=9)


if __name__ == "__main__":
    unittest.main()
