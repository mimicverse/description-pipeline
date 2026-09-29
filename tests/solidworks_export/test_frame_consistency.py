"""Frame-consistency tests with non-identity rotation + translation.

Fixture (hand-computed expectations, no helper reuse):

* CS_dof_arm (the arm link frame) = translate (0.1, 0.2, 0.3), no rotation.
* ArmA-1 placement = rotate Z 90deg, translate (0.2, 0, 0.1)
  -> in link frame: rotate Z 90deg, translate (0.1, -0.2, -0.2).
* ArmB-1 placement = rotate X 90deg, translate (0.4, 0.1, 0.2)
  -> in link frame: rotate X 90deg, translate (0.3, -0.1, -0.1).

Mass properties are part-local with SolidWorks positive products:

* ArmA-1: m=1.0, com=(0.05, 0, 0), I=[[0.01, +0.001, 0], [.., 0.02, ..], [.., 0.03]]
* ArmB-1: m=1.5, com=(0, 0.02, 0), I=diag(0.04, 0.05, 0.06)

Expected link-frame values:

* ArmA-1: com=(0.1, -0.15, -0.2); standard tensor ixy=-0.001 rotated by
  Rz(90) -> [[0.02, +0.001, 0], [+0.001, 0.01, 0], [0, 0, 0.03]]
* ArmB-1: com=(0.3, -0.1, -0.08); diag(0.04, 0.06, 0.05)
* Combined: mass=2.5, com=(0.22, -0.12, -0.128),
  inertia6=(0.07014, -0.005, -0.0144, 0.10264, -0.0036, 0.1055)
"""

import json
import math
import os
import struct
import tempfile
import unittest

from tools.solidworks_export.backends import Backend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import CadError
from tools.solidworks_export.exporter import run_export
from tools.solidworks_export.model import RawComponent, RawScene
from tools.solidworks_export.transform import from_xyz_rpy

from .helpers import make_config_dict

RZ90 = (0.0, 0.0, math.pi / 2.0)
RX90 = (math.pi / 2.0, 0.0, 0.0)


def _write_marked_triangle(path, marker):
    """Binary STL with one triangle whose first vertex is ``marker``."""

    header = b"swbridge frame fixture".ljust(80, b" ")
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", 1))
        handle.write(struct.pack("<3f", 0.0, 0.0, 1.0))
        handle.write(struct.pack("<3f", *marker))
        handle.write(struct.pack("<3f", marker[0] + 0.01, marker[1], marker[2]))
        handle.write(struct.pack("<3f", marker[0], marker[1] + 0.01, marker[2]))
        handle.write(struct.pack("<H", 0))


def _first_vertex(path):
    with open(path, "rb") as handle:
        handle.seek(84 + 12)
        return struct.unpack("<3f", handle.read(12))


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class RotatedAssemblyBackend(Backend):
    name = "fake"

    def __init__(self):
        self.components = {
            "Base-1": RawComponent(
                name="Base-1", transform=from_xyz_rpy((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)), is_fixed=True
            ),
            "ArmA-1": RawComponent(name="ArmA-1", transform=from_xyz_rpy((0.2, 0.0, 0.1), RZ90)),
            "ArmB-1": RawComponent(name="ArmB-1", transform=from_xyz_rpy((0.4, 0.1, 0.2), RX90)),
        }
        self.coordinate_systems = {
            "CS_dof_arm": from_xyz_rpy((0.1, 0.2, 0.3), (0.0, 0.0, 0.0)),
        }
        self.mass_properties = {
            "Base-1": {
                "mass": 2.0,
                "com": (0.0, 0.0, 0.05),
                "inertia": ((0.1, 0.0, 0.0), (0.0, 0.2, 0.0), (0.0, 0.0, 0.3)),
                "reference": {"used_api": "fixture"},
            },
            "ArmA-1": {
                "mass": 1.0,
                "com": (0.05, 0.0, 0.0),
                "inertia": ((0.01, 0.001, 0.0), (0.001, 0.02, 0.0), (0.0, 0.0, 0.03)),
                "reference": {"used_api": "fixture"},
            },
            "ArmB-1": {
                "mass": 1.5,
                "com": (0.0, 0.02, 0.0),
                "inertia": ((0.04, 0.0, 0.0), (0.0, 0.05, 0.0), (0.0, 0.0, 0.06)),
                "reference": {"used_api": "fixture"},
            },
        }

    def health(self):
        return {"ok": True, "backend": "fake", "sw_version": "fixture"}

    def collect_scene(self, doc_path, coordinate_systems, progress=None, require_material=True):
        available = {
            name: self.coordinate_systems[name] for name in coordinate_systems if name in self.coordinate_systems
        }
        return RawScene(
            document=doc_path,
            components=list(self.components.values()),
            coordinate_systems=available,
            mass_properties=dict(self.mass_properties),
            notes={"backend": "fixture"},
        )

    def export_component_mesh(self, component, dest_path, progress=None):
        if component not in self.components:
            raise CadError("cad_missing_component", f"unknown {component!r}")
        markers = {"Base-1": (0.0, 0.0, 0.0), "ArmA-1": (1.0, 0.0, 0.0), "ArmB-1": (0.0, 1.0, 0.0)}
        _write_marked_triangle(dest_path, markers[component])
        return {"component": component, "written": dest_path, "used_api": "fixture", "triangles": 1}


def _fixture_config():
    data = make_config_dict()
    data["links"] = [
        {"name": "base_link", "components": ["Base-1"]},
        {"name": "arm_link", "components": ["ArmA-1", "ArmB-1"], "frame_component": "ArmA-1"},
    ]
    data["joints"][0]["coordinate_system"] = "CS_dof_arm"
    return config_from_dict(data)


class FrameConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-frame-")
        self.out = os.path.join(self.tmp, "out")
        self.cfg = _fixture_config()
        run_export(RotatedAssemblyBackend(), self.cfg, self.out, "D:/models/ROTATED.SLDASM")

    def test_joint_origin_is_joint_coordinate_system(self):
        with open(os.path.join(self.out, "robot.urdf"), encoding="utf-8") as handle:
            urdf = handle.read()
        self.assertIn('xyz="0.1 0.2 0.3"', urdf)

    def test_combined_mass_properties_in_link_frame(self):
        evidence = _load_json(os.path.join(self.out, "cad_evidence.json"))
        arm = evidence["links"]["arm_link"]
        self.assertEqual(arm["link_frame_source"], "joint_coordinate_system:CS_dof_arm")
        combined = arm["combined"]
        self.assertAlmostEqual(combined["mass"], 2.5, places=12)
        for axis, expected in enumerate((0.22, -0.12, -0.128)):
            self.assertAlmostEqual(combined["com"][axis], expected, places=12)
        inertia = combined["inertia"]
        expected6 = ((0, 0, 0.07014), (0, 1, -0.005), (0, 2, -0.0144), (1, 1, 0.10264), (1, 2, -0.0036), (2, 2, 0.1055))
        for row, col, value in expected6:
            self.assertAlmostEqual(inertia[row][col], value, places=9)

    def test_raw_readings_and_convention_are_preserved(self):
        evidence = _load_json(os.path.join(self.out, "cad_evidence.json"))
        raw = evidence["links"]["arm_link"]["raw"]["ArmA-1"]
        self.assertAlmostEqual(raw["inertia"][0][1], 0.001, places=12)
        self.assertEqual(raw["product_convention"], "solidworks_positive")
        self.assertEqual(raw["raw_frame"], "component_part_frame")

    def test_meshes_are_merged_in_the_link_frame(self):
        # ArmA-1 marker (1,0,0) -> Rz90 -> (0,1,0) + (0.1,-0.2,-0.2)
        # ArmB-1 marker (0,1,0) -> Rx90 -> (0,0,1) + (0.3,-0.1,-0.1)
        arm_mesh = os.path.join(self.out, "meshes", "arm_link.STL")
        points = []
        with open(arm_mesh, "rb") as handle:
            handle.seek(84)
            data = handle.read()
        for offset in range(0, len(data), 50):
            points.append(struct.unpack_from("<3f", data, offset + 12))
        self.assertEqual(len(points), 2)
        expected = ((0.1, 0.8, -0.2), (0.3, -0.1, 0.9))
        for point, target in zip(points, expected, strict=True):
            for axis in range(3):
                self.assertAlmostEqual(point[axis], target[axis], places=5)

    def test_sidecar_matches_link_frame_values(self):
        sidecar = _load_json(os.path.join(self.out, "cad_inertia_evidence.json"))
        record = sidecar["links"]["arm_link"]
        self.assertEqual(record["product_convention"], "negative_products")
        self.assertFalse(record["frame_mapping_confirmed"])
        self.assertFalse(record["component_selection_confirmed"])
        self.assertAlmostEqual(record["mass_kg"], 2.5, places=12)
        self.assertAlmostEqual(record["L_at_com"]["ixy"], -0.005, places=9)
        self.assertEqual(record["output_frame_in_link"], {"xyz_m": [0.0, 0.0, 0.0], "rpy_rad": [0.0, 0.0, 0.0]})


if __name__ == "__main__":
    unittest.main()
