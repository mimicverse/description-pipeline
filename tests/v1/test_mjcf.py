"""URDF to MJCF projection, support derivation, sealing and adversarial checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np

from description_pipeline import mjcf
from description_pipeline import mjcf_contract as contract
from description_pipeline.io import PipelineError
from description_pipeline.verification import mjcf as verifier

REPO_ROOT = Path(__file__).resolve().parents[2]


def stl(triangles) -> bytes:
    payload = bytearray(b"\0" * 80)
    payload += struct.pack("<I", len(triangles))
    for a, b, c in triangles:
        payload += struct.pack("<3f", 0.0, 0.0, 0.0)
        for vertex in (a, b, c):
            payload += struct.pack("<3f", *vertex)
        payload += struct.pack("<H", 0)
    return bytes(payload)


def pyramid_stl() -> bytes:
    vertices = [
        (0.0, 0.0, 0.0),
        (0.2, 0.0, 0.0),
        (0.2, 0.1, 0.0),
        (0.0, 0.1, 0.0),
        (0.1, 0.05, 0.08),
    ]
    faces = [(0, 1, 2), (0, 2, 3), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    return stl([(vertices[a], vertices[b], vertices[c]) for a, b, c in faces])


def tetra_stl() -> bytes:
    vertices = [(0.0, 0.0, 0.0), (0.03, 0.002, 0.0001), (0.005, 0.04, 0.001), (0.011, 0.009, 0.05)]
    faces = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    return stl([(vertices[a], vertices[b], vertices[c]) for a, b, c in faces])


def v_tie_stl() -> bytes:
    """Two symmetric equal-area plates meeting at one edge: a support tie."""

    def quad(second):
        return [
            ((0.0, 0.0, 0.0), (second[0], 0.0, second[1]), (second[0], 0.05, second[1])),
            ((0.0, 0.0, 0.0), (second[0], 0.05, second[1]), (0.0, 0.05, 0.0)),
        ]

    triangles = quad((0.1, 0.1)) + quad((-0.1, 0.1))
    return stl(triangles)


def fins_stl() -> bytes:
    """Three plates sharing one edge at 0 / 120 / 240 degrees: no supporting plane."""

    import math

    triangles = []
    for angle in (0.0, 2 * math.pi / 3, 4 * math.pi / 3):
        x, z = 0.1 * math.cos(angle), 0.1 * math.sin(angle)
        triangles += [
            ((0.0, 0.0, 0.0), (x, 0.0, z), (x, 0.05, z)),
            ((0.0, 0.0, 0.0), (x, 0.05, z), (0.0, 0.05, 0.0)),
        ]
    return stl(triangles)


def vertical_plate_stl() -> bytes:
    return stl(
        [
            ((0.0, 0.0, 0.0), (0.0, 0.1, 0.0), (0.0, 0.1, 0.1)),
            ((0.0, 0.0, 0.0), (0.0, 0.1, 0.1), (0.0, 0.0, 0.1)),
        ]
    )


def far_point_stl() -> bytes:
    """A pyramid on a flat base plus a distant sharp point touching the same plane."""

    vertices = [
        (0.0, 0.0, 0.0),
        (0.2, 0.0, 0.0),
        (0.2, 0.1, 0.0),
        (0.0, 0.1, 0.0),
        (0.1, 0.05, 0.08),
        (1.0, 0.05, 0.0),
        (1.05, 0.02, 0.03),
        (1.05, 0.08, 0.03),
        (1.0, 0.05, 0.06),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (0, 1, 4),
        (1, 2, 4),
        (2, 3, 4),
        (3, 0, 4),
        (5, 6, 7),
        (5, 7, 8),
        (5, 8, 6),
        (6, 7, 8),
    ]
    return stl([(vertices[a], vertices[b], vertices[c]) for a, b, c in faces])


URDF = """<?xml version="1.0"?>
<robot name="fixture">
  <material name="gray"><color rgba="0.6 0.6 0.62 1"/></material>
  <link name="base_link">
    <inertial>
      <origin xyz="0.01 -0.002 0.02" rpy="0.3 -0.2 0.1"/>
      <mass value="1.5"/>
      <inertia ixx="0.002" ixy="0.0002" ixz="-0.0001" iyy="0.003" iyz="0.0003" izz="0.004"/>
    </inertial>
    <visual name="body">
      <origin xyz="0 0 0.22"/>
      <geometry><box size="0.2 0.1 0.1"/></geometry>
      <material name="gray"/>
    </visual>
    <visual name="foot">
      <origin xyz="0 0 0.05" rpy="0.12 0 0"/>
      <geometry><mesh filename="../meshes/foot.stl"/></geometry>
    </visual>
    <collision name="pad_collision">
      <origin xyz="0.05 0 0.18"/>
      <geometry><mesh filename="../meshes/collision_only.stl"/></geometry>
    </collision>
  </link>
  <link name="arm_link">
    <inertial>
      <origin xyz="0.05 0.01 0.02" rpy="0.4 -0.3 0.2"/>
      <mass value="0.4"/>
      <inertia ixx="0.0004" ixy="0.00003" ixz="-0.00002" iyy="0.0005" iyz="0.00001" izz="0.0006"/>
    </inertial>
    <visual name="arm">
      <origin xyz="0.05 0 0" rpy="0 0.5 0"/>
      <geometry><cylinder radius="0.02" length="0.12"/></geometry>
      <material name="inline"><color rgba="0.9 0.2 0.2 1"/></material>
    </visual>
    <visual name="pad">
      <origin xyz="0.1 0.02 0.03" rpy="0.1 0.2 0.3"/>
      <geometry><mesh filename="../meshes/pad.stl" scale="0.5 2 1"/></geometry>
    </visual>
    <collision name="arm_collision">
      <origin xyz="0.05 0 0"/>
      <geometry><sphere radius="0.03"/></geometry>
    </collision>
    <collision name="arm_collision_2">
      <origin xyz="0.1 0 0"/>
      <geometry><box size="0.04 0.04 0.04"/></geometry>
    </collision>
  </link>
  <link name="hand_link">
    <inertial>
      <origin xyz="0.03 0 0.01" rpy="-0.2 0.1 0.5"/>
      <mass value="0.15"/>
      <inertia ixx="0.0002" ixy="0.000001" ixz="0.0000005" iyy="0.00020002" iyz="-0.000001" izz="0.0003"/>
    </inertial>
    <visual name="palm">
      <geometry><mesh filename="../meshes/pad.stl"/></geometry>
    </visual>
    <visual name="grip">
      <origin xyz="0.06 0 0"/>
      <geometry><sphere radius="0.02"/></geometry>
    </visual>
    <collision name="palm_collision">
      <geometry><box size="0.05 0.06 0.02"/></geometry>
    </collision>
  </link>
  <link name="tool_link">
    <inertial>
      <origin xyz="0.02 0 0.01" rpy="0.1 -0.4 0.7"/>
      <mass value="0.05"/>
      <inertia ixx="0.000001" ixy="0.00000005" ixz="-0.00000003" iyy="0.000002" iyz="0.00000002" izz="0.0000025"/>
    </inertial>
    <visual name="tool">
      <origin xyz="0.03 0 0"/>
      <geometry><cylinder radius="0.008" length="0.06"/></geometry>
    </visual>
    <collision name="tool_collision">
      <geometry><sphere radius="0.012"/></geometry>
    </collision>
  </link>
  <link name="flag_link"/>
  <joint name="shoulder" type="revolute">
    <origin xyz="0 0 0.25" rpy="0.2 0 0"/>
    <parent link="base_link"/>
    <child link="arm_link"/>
    <axis xyz="0 3 4"/>
    <limit lower="-1.3" upper="0.45" effort="30" velocity="2"/>
    <dynamics damping="0.02" friction="0.004"/>
  </joint>
  <joint name="wrist" type="continuous">
    <origin xyz="0.15 0 0" rpy="0 0.3 0"/>
    <parent link="arm_link"/>
    <child link="hand_link"/>
    <axis xyz="0 -2 0"/>
  </joint>
  <joint name="slider" type="prismatic">
    <origin xyz="0.1 0 0" rpy="0 -0.7 0"/>
    <parent link="hand_link"/>
    <child link="tool_link"/>
    <axis xyz="1 2 2"/>
    <limit lower="-0.05" upper="0.08" effort="5" velocity="0.1"/>
    <dynamics friction="0.01"/>
  </joint>
  <joint name="flag_mount" type="fixed">
    <origin xyz="0.05 0 0"/>
    <parent link="tool_link"/>
    <child link="flag_link"/>
  </joint>
</robot>
"""


def configuration(**overrides) -> dict:
    config = {
        "schema_version": 1,
        "base": "floating",
        "collision": {"source": "visual_convex", "self_collision": True, "friction": [0.8, 0.005, 0.0001]},
        "simulation": {"timestep": 0.001, "gravity": [0, 0, -9.81], "integrator": "implicitfast"},
        "sites": [
            {"name": "tool_mark", "link": "tool_link", "xyz": [0.01, 0, 0.02], "rpy": [0.2, 0.1, -0.3]}
        ],
        "support_sites": [{"name": "left_sole", "link": "base_link"}],
        "floor": {"height": "supports", "friction": [0.9, 0.005, 0.0001]},
    }
    for key, value in overrides.items():
        config[key] = value
    return config


class _Source:
    """One in-memory source tree plus its configuration file."""

    def __init__(
        self,
        base: Path,
        *,
        urdf: str = URDF,
        config: dict | None = None,
        config_bytes: bytes | None = None,
        meshes: dict | None = None,
    ):
        self.root = base / "source"
        (self.root / "urdf").mkdir(parents=True)
        (self.root / "meshes").mkdir()
        (self.root / "urdf/robot.urdf").write_text(urdf)
        payloads = {"foot.stl": pyramid_stl(), "pad.stl": tetra_stl(), "collision_only.stl": tetra_stl()}
        if meshes:
            payloads.update(meshes)
        for name, content in payloads.items():
            (self.root / "meshes" / name).write_bytes(content)
        self.config_path = base / "config.json"
        if config_bytes is not None:
            self.config_path.write_bytes(config_bytes)
        else:
            self.config_path.write_text(json.dumps(configuration() if config is None else config))

    def build(self, output: str = "delivery", urdf: str = "urdf/robot.urdf", config: bool = True) -> Path:
        target = self.config_path.parent / output
        mjcf.build(self.root, urdf, self.config_path if config else None, target)
        return target


def digest_data(data) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def reseal_manifest(bundle: Path) -> str:
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["files"] = {
        relative: {
            "bytes": len((bundle / relative).read_bytes()),
            "sha256": hashlib.sha256((bundle / relative).read_bytes()).hexdigest(),
        }
        for relative in manifest["files"]
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    return digest_data(manifest)


def rewrite_robot(bundle: Path, editor) -> None:
    path = bundle / "robot.xml"
    root = ET.fromstring(path.read_bytes())
    editor(root)
    ET.indent(root, space="  ")
    path.write_bytes(f'<?xml version="1.0" encoding="UTF-8"?>\n{ET.tostring(root, encoding="unicode")}\n'.encode())


def body_named(root: ET.Element, name: str) -> ET.Element:
    return next(element for element in root.iter("body") if element.get("name") == name)


class ConfigurationTests(unittest.TestCase):
    def test_the_supported_configuration_round_trips(self):
        config = contract.load_simulation(json.dumps(configuration()).encode())
        self.assertEqual(config["base"], "floating")
        self.assertEqual(config["collision"]["source"], "visual_convex")
        self.assertEqual(config["support_sites"], [{"name": "left_sole", "link": "base_link"}])
        self.assertTrue(config["floor"]["supports"])
        self.assertIsNone(config["floor"]["height"])

    def test_configuration_rejections(self):
        base = configuration()

        def mutate(editor) -> bytes:
            document = json.loads(json.dumps(base))
            editor(document)
            return json.dumps(document).encode()

        cases = {
            "unknown key": lambda document: document.update({"extra": 1}),
            "schema": lambda document: document.update({"schema_version": 2}),
            "base enum": lambda document: document.update({"base": "hovering"}),
            "source enum": lambda document: document["collision"].update({"source": "mesh"}),
            "integrator enum": lambda document: document["simulation"].update({"integrator": "euler"}),
            "negative friction": lambda document: document["collision"].update({"friction": [-0.1, 0, 0]}),
            "zero timestep": lambda document: document["simulation"].update({"timestep": 0}),
            "site name clash": lambda document: document["sites"].append(
                {"name": "left_sole", "link": "base_link", "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
            ),
            "support without source": lambda document: document["collision"].update({"source": "none"}),
            "supports without sites": lambda document: document.update({"support_sites": []}),
            "site extra key": lambda document: document["sites"][0].update({"extra": 1}),
        }
        for label, editor in cases.items():
            with self.subTest(label=label), self.assertRaises(PipelineError):
                contract.load_simulation(mutate(editor))
        with self.assertRaises(PipelineError) as caught:
            contract.load_simulation(b'{"base": "fixed", "base": "floating"}')
        self.assertIn("Duplicate input key", str(caught.exception))
        with self.assertRaises(PipelineError) as caught:
            contract.load_simulation(b'{"simulation": {"timestep": 1e999}}')
        self.assertIn("not finite", str(caught.exception))


class MinimalConfigurationTests(unittest.TestCase):
    def test_omitted_fields_resolve_to_the_versioned_defaults(self):
        expected = {
            "schema_version": 1,
            "base": "floating",
            "collision": {"source": "urdf", "self_collision": True, "friction": (0.8, 0.005, 0.0001)},
            "simulation": {"timestep": 0.001, "gravity": (0.0, 0.0, -9.81), "integrator": "implicitfast"},
            "sites": [],
            "support_sites": [],
            "floor": None,
        }
        for content in (b"{}", b"{}\n"):
            with self.subTest(content=content):
                self.assertEqual(contract.load_simulation(content), expected)

    def test_partial_overrides_keep_their_siblings(self):
        config = contract.load_simulation(
            json.dumps(
                {"collision": {"friction": [0.9, 0.02, 0.002]}, "simulation": {"integrator": "Euler"}}
            ).encode()
        )
        self.assertEqual(
            config["collision"], {"source": "urdf", "self_collision": True, "friction": (0.9, 0.02, 0.002)}
        )
        self.assertEqual(
            config["simulation"], {"timestep": 0.001, "gravity": (0.0, 0.0, -9.81), "integrator": "Euler"}
        )

    def test_explicit_nulls_are_never_omissions(self):
        cases = (
            b'{"schema_version": null}',
            b'{"base": null}',
            b'{"collision": null}',
            b'{"collision": {"source": null}}',
            b'{"collision": {"friction": null}}',
            b'{"simulation": null}',
            b'{"simulation": {"gravity": null}}',
            b'{"sites": null}',
            b'{"sites": [{"name": "a", "link": "b", "xyz": null}]}',
            b'{"support_sites": null}',
            b'{"support_sites": [{"link": null}]}',
            b'{"floor": null}',
            b'{"floor": {"height": null}}',
            b'{"floor": {"friction": null}}',
        )
        for content in cases:
            with self.subTest(content=content), self.assertRaises(PipelineError):
                contract.load_simulation(content)

    def test_bad_types_are_rejected(self):
        cases = (
            b'{"sites": {}}',
            b'{"sites": ["plain"]}',
            b'{"sites": [{"name": "a", "link": "b", "xyz": "0 0 0"}]}',
            b'{"sites": [{"name": 1, "link": "b"}]}',
            b'{"support_sites": {}}',
            b'{"support_sites": [{"name": "a"}]}',
            b'{"collision": {"self_collision": "true"}}',
            b'{"floor": []}',
            b'{"schema_version": true}',
        )
        for content in cases:
            with self.subTest(content=content), self.assertRaises(PipelineError):
                contract.load_simulation(content)

    def test_support_names_default_and_conflicts_are_unified(self):
        config = contract.load_simulation(b'{"support_sites": [{"link": "left_foot"}]}')
        self.assertEqual(config["support_sites"], [{"name": "left_foot__support", "link": "left_foot"}])
        clashes = (
            b'{"sites": [{"name": "left_foot__support", "link": "x"}], "support_sites": [{"link": "left_foot"}]}',
            b'{"support_sites": [{"link": "left_foot"}, {"link": "left_foot"}]}',
            b'{"support_sites": [{"link": "left_foot", "name": "sole"}, {"link": "hand", "name": "sole"}]}',
        )
        for content in clashes:
            with self.subTest(content=content), self.assertRaises(PipelineError):
                contract.load_simulation(content)

    def test_floor_defaults_inherit_and_stay_decoupled(self):
        config = contract.load_simulation(b'{"floor": {}}')
        self.assertEqual(
            config["floor"], {"height": 0.0, "supports": False, "friction": (0.8, 0.005, 0.0001)}
        )
        inherited = contract.load_simulation(b'{"floor": {}, "collision": {"friction": [0.9, 0.02, 0.002]}}')
        self.assertEqual(inherited["floor"]["friction"], (0.9, 0.02, 0.002))
        supported = contract.load_simulation(b'{"floor": {}, "support_sites": [{"link": "foot"}]}')
        self.assertTrue(supported["floor"]["supports"])
        self.assertIsNone(supported["floor"]["height"])
        self.assertIsNone(contract.load_simulation(b'{"support_sites": [{"link": "foot"}]}')["floor"])
        with self.assertRaises(PipelineError):
            contract.load_simulation(b'{"floor": {"height": "supports"}}')

    def test_sites_default_to_the_link_frame(self):
        config = contract.load_simulation(b'{"sites": [{"name": "mark", "link": "tool"}]}')
        self.assertEqual(
            config["sites"],
            [{"name": "mark", "link": "tool", "xyz": (0.0, 0.0, 0.0), "rpy": (0.0, 0.0, 0.0)}],
        )


class ProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mjcf-projection-")
        cls.base = Path(cls.temporary.name)
        cls.source = _Source(cls.base)
        source_inventory = dict(_walk(cls.source.root))
        cls.bundle = cls.source.build()
        cls.after_inventory = _walk(cls.source.root)
        cls.before_inventory = source_inventory
        cls.report = verifier.check_bundle(cls.bundle)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_every_gate_passes_with_expected_facts(self):
        report = self.report
        self.assertTrue(report["passed"], report["checks"])
        self.assertEqual([check["id"] for check in report["checks"]], list(contract.CHECK_IDS))
        facts = report["facts"]
        self.assertEqual(facts["links"], 5)
        self.assertEqual(facts["joints"], 4)
        self.assertEqual(facts["visuals"], 7)
        self.assertEqual(facts["collision_source"], "visual_convex")
        self.assertEqual(facts["collision_geoms"], 7)
        self.assertEqual(facts["support_sites"], 1)
        self.assertEqual(facts["base"], "floating")
        effort = {row["joint"]: (row["effort"], row["velocity"]) for row in facts["effort_velocity"]}
        self.assertEqual(effort["shoulder"], (30.0, 2.0))
        self.assertEqual(effort["wrist"], (None, None))
        self.assertEqual(effort["slider"], (5.0, 0.1))
        self.assertEqual(effort["flag_mount"], (None, None))

    def test_delivery_contents_and_manifest(self):
        expected = {
            "input/urdf/robot.urdf",
            "input/meshes/collision_only.stl",
            "input/meshes/foot.stl",
            "input/meshes/pad.stl",
            "simulation.json",
            "robot.xml",
            "scene.xml",
            "manifest.json",
            "reports/quality.json",
        }
        self.assertEqual(set(_walk(self.bundle)), expected)
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], contract.MANIFEST_SCHEMA)
        self.assertEqual(manifest["source"]["urdf"], "urdf/robot.urdf")
        self.assertEqual(
            manifest["source"]["meshes"], ["meshes/collision_only.stl", "meshes/foot.stl", "meshes/pad.stl"]
        )
        self.assertEqual(manifest["engine"], {"mujoco": "3.13.0"})
        self.assertEqual(manifest["tool"], contract.tool_identity())
        for relative, record in manifest["files"].items():
            content = (self.bundle / relative).read_bytes()
            self.assertEqual(record["bytes"], len(content))
            self.assertEqual(record["sha256"], hashlib.sha256(content).hexdigest())

    def test_robot_xml_roles_unbaked_axes_and_inertia(self):
        robot = ET.fromstring((self.bundle / "robot.xml").read_bytes())
        self.assertEqual(robot.get("model"), "fixture")
        compiler = robot.find("compiler")
        self.assertEqual(compiler.get("angle"), "radian")
        self.assertEqual(compiler.get("fusestatic"), "false")
        self.assertEqual(compiler.get("inertiafromgeom"), "false")
        geoms = {geom.get("name"): geom for geom in robot.iter("geom")}
        self.assertEqual(geoms["arm_link__visual_0"].get("group"), "2")
        self.assertEqual(geoms["arm_link__visual_0"].get("contype"), "0")
        self.assertEqual(geoms["arm_link__convex_0"].get("group"), "3")
        self.assertEqual(geoms["arm_link__convex_0"].get("contype"), "1")
        self.assertEqual(geoms["arm_link__convex_0"].get("conaffinity"), "1")
        self.assertEqual(geoms["base_link__visual_1"].get("type"), "mesh")
        jobs = {
            joint.get("name"): joint
            for joint in robot.iter("joint")
        }
        self.assertEqual(jobs["shoulder"].get("axis"), "0 0.6 0.8")
        self.assertEqual(jobs["wrist"].get("axis"), "0 -1.0 0")
        self.assertEqual(jobs["wrist"].get("limited"), "false")
        self.assertEqual(jobs["wrist"].get("range"), None)
        self.assertAlmostEqual(float(jobs["shoulder"].get("damping")), 0.02, places=15)
        scales = [(mesh.get("file"), mesh.get("scale")) for mesh in robot.find("asset").findall("mesh")]
        self.assertIn(("input/meshes/pad.stl", "0.5 2.0 1.0"), scales)
        self.assertIn(("input/meshes/pad.stl", None), scales)
        self.assertIn(("input/meshes/foot.stl", None), scales)
        sites = {site.get("name"): site for site in robot.iter("site")}
        self.assertIn("left_sole", sites)
        self.assertIn("tool_mark", sites)
        # Off-diagonal inertia survives: the stored principal values rebuild the rotated tensor.
        base = body_named(robot, "base_link").find("inertial")
        self.assertIn("diaginertia", base.attrib)
        self.assertNotIn("fullinertia", base.attrib)

    def test_derived_support_site_and_floor_height(self):
        import math

        import numpy as np

        robot = ET.fromstring((self.bundle / "robot.xml").read_bytes())
        site = next(element for element in robot.iter("site") if element.get("name") == "left_sole")
        tilt = 0.12
        quad = np.array([[0, 0, 0], [0.2, 0, 0], [0.2, 0.1, 0], [0, 0.1, 0]], dtype=np.float32).astype(np.float64)
        centre = quad.mean(axis=0)
        rotation = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, math.cos(tilt), -math.sin(tilt)],
                [0.0, math.sin(tilt), math.cos(tilt)],
            ]
        )
        expected_position = rotation @ centre + np.array([0.0, 0.0, 0.05])
        actual_position = [float(value) for value in site.get("pos").split()]
        for axis in range(3):
            self.assertAlmostEqual(actual_position[axis], expected_position[axis], delta=1e-9)
        expected_quat = contract.quaternion_from_matrix(rotation)
        actual_quat = [float(value) for value in site.get("quat").split()]
        for left, right in zip(actual_quat, expected_quat, strict=True):
            self.assertAlmostEqual(left, right, delta=1e-9)
        scene = ET.fromstring((self.bundle / "scene.xml").read_bytes())
        floor = next(element for element in scene.iter("geom") if element.get("name") == contract.FLOOR_GEOM_NAME)
        self.assertEqual(floor.get("pos").split(), ["0", "0", contract_format(0.05)])
        scene_check = next(check for check in self.report["checks"] if check["id"] == "scene.scene")
        self.assertAlmostEqual(scene_check["details"]["height"], 0.05, places=12)

    def test_check_binding_and_read_only_behaviour(self):
        before = _walk(self.bundle)
        again = verifier.check_bundle(self.bundle)
        self.assertTrue(again["passed"])
        self.assertEqual(_walk(self.bundle), before)
        saved = json.loads((self.bundle / "reports/quality.json").read_text())
        self.assertEqual(saved, json.loads(json.dumps(again)))

    def test_the_source_tree_is_untouched(self):
        self.assertEqual(self.before_inventory, self.after_inventory)
        self.assertEqual(
            self.before_inventory["urdf/robot.urdf"],
            hashlib.sha256((self.source.root / "urdf/robot.urdf").read_bytes()).hexdigest(),
        )

    def test_two_builds_in_different_directories_are_byte_identical(self):
        second = self.source.build("delivery-b")
        first_walk, second_walk = _walk(self.bundle), _walk(second)
        self.assertEqual(first_walk, second_walk)
        for relative in first_walk:
            self.assertEqual((self.bundle / relative).read_bytes(), (second / relative).read_bytes(), relative)

    def test_no_temporary_directories_remain(self):
        leftovers = sorted(path.name for path in self.base.iterdir() if path.name.startswith(".mjcf-build-"))
        self.assertEqual(leftovers, [])


def contract_format(value: float) -> str:
    from description_pipeline.io import format_number

    return format_number(value)


def _walk(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def expect_build_error(case, *, urdf=URDF, config=None, meshes=None, message: str) -> None:
    with tempfile.TemporaryDirectory(prefix="mjcf-negative-") as directory:
        source = _Source(Path(directory), urdf=urdf, config=config, meshes=meshes)
        with case.assertRaises(PipelineError) as caught:
            source.build()
        case.assertIn(message, str(caught.exception))


class NegativeFixtureTests(unittest.TestCase):
    def test_urdf_surface_rejections(self):
        def replace(old, new):
            return URDF.replace(old, new)

        cases = {
            "mimic": (
                replace('<axis xyz="0 -2 0"/>', '<axis xyz="0 -2 0"/><mimic joint="shoulder"/>'),
                "unsupported element",
            ),
            "transmission": (
                replace("</robot>", '<transmission name="t"><type>simple</type></transmission></robot>'),
                "unsupported element",
            ),
            "unknown joint attribute": (
                replace(
                    '<joint name="wrist" type="continuous">',
                    '<joint name="wrist" type="continuous" extra="1">',
                ),
                "unsupported attribute",
            ),
            "missing limit": (
                replace(
                    '<axis xyz="1 2 2"/>\n'
                    '    <limit lower="-0.05" upper="0.08" effort="5" velocity="0.1"/>',
                    '<axis xyz="1 2 2"/>',
                ),
                "requires <limit>",
            ),
            "invalid range": (
                replace('lower="-1.3" upper="0.45"', 'lower="0.45" upper="-1.3"'),
                "strictly increasing",
            ),
            "zero axis": (replace('<axis xyz="1 2 2"/>', '<axis xyz="0 0 0"/>'), "must be nonzero"),
            "non-finite axis": (replace('<axis xyz="0 3 4"/>', '<axis xyz="nan 0 0"/>'), "not finite"),
            "massless movable link": (
                replace(
                    """    <inertial>
      <origin xyz="0.05 0.01 0.02" rpy="0.4 -0.3 0.2"/>
      <mass value="0.4"/>
      <inertia ixx="0.0004" ixy="0.00003" ixz="-0.00002" iyy="0.0005" iyz="0.00001" izz="0.0006"/>
    </inertial>
""",
                    "",
                ),
                "no <inertial>",
            ),
            "multiple parents": (
                replace(
                    "</robot>",
                    '<joint name="extra" type="fixed"><origin xyz="0 0 0"/>'
                    '<parent link="hand_link"/><child link="tool_link"/></joint></robot>',
                ),
                "multiple parent joints",
            ),
            "duplicate link name": (
                replace('<link name="flag_link"/>', '<link name="flag_link"/>\n  <link name="flag_link"/>'),
                "duplicate link name",
            ),
            "unknown material": (
                replace('<material name="gray"/>', '<material name="missing"/>'),
                "unknown material reference",
            ),
            "absolute mesh": (
                replace('filename="../meshes/foot.stl"', 'filename="/etc/foot.stl"'),
                "relative POSIX path",
            ),
            "package uri": (
                replace('filename="../meshes/foot.stl"', 'filename="package://demo/foot.stl"'),
                "not supported",
            ),
            "path escape": (
                replace('filename="../meshes/foot.stl"', 'filename="../../meshes/foot.stl"'),
                "escapes the input root",
            ),
            "negative scale": (replace('scale="0.5 2 1"', 'scale="-0.5 2 1"'), "mesh scale must be positive"),
            "unphysical inertia": (
                replace(
                    'ixx="0.002" ixy="0.0002" ixz="-0.0001" iyy="0.003" iyz="0.0003" izz="0.004"',
                    'ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.01"',
                ),
                "principal moments violate",
            ),
            "negative mass": (replace('<mass value="0.4"/>', '<mass value="-0.4"/>'), "mass must be positive"),
            "case conflict": (
                replace(
                    '<visual name="foot">',
                    '<visual name="upper_foot"><origin xyz="0 0 0.3"/>'
                    '<geometry><mesh filename="../meshes/Foot.stl"/></geometry></visual>\n    <visual name="foot">',
                ),
                "case-conflicting input paths",
            ),
        }
        for label, (text, message) in cases.items():
            with self.subTest(label=label):
                expect_build_error(self, urdf=text, message=message)

    def test_mesh_and_structure_rejections(self):
        cases = {
            "truncated stl": ({"foot.stl": pyramid_stl()[:50]}, "not a binary STL"),
            "ascii stl": ({"foot.stl": b"solid x\nfacet normal 0 0 0\nendsolid x\n"}, "not a binary STL"),
            "empty stl": ({"foot.stl": stl([])}, "mesh is empty"),
            "tie": ({"foot.stl": v_tie_stl()}, "same contact area"),
            "no support plane": ({"foot.stl": fins_stl()}, "no triangle plane lies below"),
            "vertical plate": ({"foot.stl": vertical_plate_stl()}, "upward-facing support plane candidate"),
        }
        for label, (meshes, message) in cases.items():
            with self.subTest(label=label):
                expect_build_error(self, meshes=meshes, message=message)

    def test_missing_mesh_and_structure_rejections(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-negative-") as directory:
            source = _Source(Path(directory))
            (source.root / "meshes/foot.stl").unlink()
            with self.assertRaises(PipelineError) as caught:
                source.build()
            self.assertIn("Missing artifact", str(caught.exception))
        # Two roots: the shoulder joint is removed and the chain disconnects.
        text = URDF.replace(
            """  <joint name="shoulder" type="revolute">
    <origin xyz="0 0 0.25" rpy="0.2 0 0"/>
    <parent link="base_link"/>
    <child link="arm_link"/>
    <axis xyz="0 3 4"/>
    <limit lower="-1.3" upper="0.45" effort="30" velocity="2"/>
    <dynamics damping="0.02" friction="0.004"/>
  </joint>
""",
            "",
        )
        expect_build_error(self, urdf=text, message="root links")
        # Duplicate configuration keys and non-finite numbers are refused before any projection.
        with self.assertRaises(PipelineError) as caught:
            mjcf.build(
                Path("/nonexistent"),
                "urdf/robot.urdf",
                Path("/nonexistent/config.json"),
                Path("/nonexistent/out"),
            )
        self.assertIn("not an existing directory", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "symlinks are a POSIX fixture")
    def test_symlinked_mesh_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-negative-") as directory:
            source = _Source(Path(directory))
            target = source.root / "meshes/foot.stl"
            target.unlink()
            os.symlink("pad.stl", target)
            with self.assertRaises(PipelineError) as caught:
                source.build()
            self.assertIn("Symlink", str(caught.exception))

    def test_output_and_support_selection_rejections(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-negative-") as directory:
            source = _Source(Path(directory))
            source.build()
            with self.assertRaises(PipelineError) as caught:
                source.build()
            self.assertIn("Refusing to overwrite", str(caught.exception))
            with self.assertRaises(PipelineError) as caught:
                mjcf.build(source.root, "urdf/robot.urdf", source.config_path, source.root / "inside")
            self.assertIn("outside the input root", str(caught.exception))
            with self.assertRaises(PipelineError) as caught:
                mjcf.build(
                    source.root,
                    "urdf/robot.urdf",
                    source.config_path.parent / "missing.json",
                    Path(directory) / "other",
                )
            self.assertIn("is not a file", str(caught.exception))
        expect_build_error(
            self,
            config=configuration(support_sites=[{"name": "sole", "link": "tool_link"}]),
            message="has no binary STL",
        )
        expect_build_error(
            self,
            config=configuration(support_sites=[{"name": "sole", "link": "nowhere"}]),
            message="unknown link",
        )


class AdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mjcf-adversarial-")
        cls.base = Path(cls.temporary.name)
        cls.source = _Source(cls.base)
        cls.original = cls.source.build("original")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def tamper(self, name: str) -> Path:
        target = self.base / name
        shutil.copytree(self.original, target)
        return target

    def assert_gate_failed(self, bundle: Path, gate: str):
        report = verifier.check_bundle(bundle)
        self.assertFalse(report["passed"], report["checks"])
        failed = [check["id"] for check in report["checks"] if check["passed"] is False]
        self.assertIn(gate, failed, failed)
        return report

    def test_resealed_inertia_reordering_is_rejected_semantically(self):
        bundle = self.tamper("inertia")

        def editor(root):
            inertial = body_named(root, "arm_link").find("inertial")
            values = inertial.get("diaginertia").split()
            inertial.set("diaginertia", " ".join([values[2], values[1], values[0]]))

        rewrite_robot(bundle, editor)
        manifest_digest = reseal_manifest(bundle)
        saved = json.loads((bundle / "reports/quality.json").read_text())
        saved["manifest_sha256"] = manifest_digest
        (bundle / "reports/quality.json").write_text(json.dumps(saved, sort_keys=True, separators=(",", ":")))
        self.assert_gate_failed(bundle, "inertia.links")

    def test_resealed_inertia_rotation_is_rejected_semantically(self):
        bundle = self.tamper("inertia-rotation")

        def editor(root):
            body_named(root, "base_link").find("inertial").set("quat", "1 0 0 0")

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "inertia.links")

    def test_resealed_axis_tamper_is_rejected_semantically(self):
        bundle = self.tamper("axis")

        def editor(root):
            joint = next(element for element in root.iter("joint") if element.get("name") == "shoulder")
            joint.set("axis", "0 0 1")

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "kinematics.axes")

    def test_resealed_parent_child_tamper_is_rejected_semantically(self):
        bundle = self.tamper("topology")

        def editor(root):
            arm = body_named(root, "arm_link")
            hand = body_named(root, "hand_link")
            arm.remove(hand)
            body_named(root, "base_link").append(hand)

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "topology.entities")

    def test_resealed_geometry_tamper_is_rejected_semantically(self):
        bundle = self.tamper("geometry")

        def editor(root):
            for mesh in root.find("asset").findall("mesh"):
                if mesh.get("file") == "input/meshes/pad.stl" and mesh.get("scale"):
                    mesh.set("scale", "0.25 2.0 1.0")

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "geometry.instances")

    def test_resealed_collision_role_tamper_is_rejected_semantically(self):
        bundle = self.tamper("collision")

        def editor(root):
            geom = next(element for element in root.iter("geom") if element.get("name") == "arm_link__visual_0")
            geom.set("contype", "1")

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "geometry.shapes")

    def test_resealed_scene_tamper_is_rejected_semantically(self):
        bundle = self.tamper("scene")
        path = bundle / "scene.xml"
        root = ET.fromstring(path.read_bytes())
        floor = next(element for element in root.iter("geom") if element.get("name") == contract.FLOOR_GEOM_NAME)
        height = float(floor.get("pos").split()[2])
        floor.set("pos", f"0 0 {height + 0.01}")
        ET.indent(root, space="  ")
        path.write_bytes(f'<?xml version="1.0" encoding="UTF-8"?>\n{ET.tostring(root, encoding="unicode")}\n'.encode())
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "scene.scene")

    def test_unregistered_extra_file_fails_closure(self):
        bundle = self.tamper("extra")
        (bundle / "notes.txt").write_text("unexpected")
        report = self.assert_gate_failed(bundle, "identity.closure")
        engine = next(check for check in report["checks"] if check["id"] == "engine.load")
        self.assertIn("closure did not pass", engine["details"]["error"])

    def test_changed_bytes_without_resealing_fail_the_sealed_record(self):
        bundle = self.tamper("digest")
        path = bundle / "robot.xml"
        path.write_bytes(path.read_bytes() + b"<!-- tampered -->\n")
        self.assert_gate_failed(bundle, "identity.closure")

    def test_missing_delivered_mesh_fails_closure(self):
        bundle = self.tamper("missing")
        (bundle / "input/meshes/foot.stl").unlink()
        self.assert_gate_failed(bundle, "identity.closure")

    def test_edited_saved_report_fails_binding(self):
        bundle = self.tamper("report")
        saved = json.loads((bundle / "reports/quality.json").read_text())
        saved["scope"] = "quietly re-scoped"
        (bundle / "reports/quality.json").write_text(json.dumps(saved, sort_keys=True, separators=(",", ":")))
        self.assert_gate_failed(bundle, "report.binding")

    def test_a_resealed_configuration_tamper_is_still_rejected(self):
        swapped = self.tamper("config-swap")
        (swapped / "simulation.json").write_text(json.dumps({"collision": {"source": "urdf"}}))
        reseal_manifest(swapped)
        self.assert_gate_failed(swapped, "geometry.shapes")
        unknown = self.tamper("config-unknown")
        (unknown / "simulation.json").write_text(json.dumps({"extra": 1}))
        reseal_manifest(unknown)
        self.assert_gate_failed(unknown, "identity.closure")

    def test_resealed_dynamics_injections_are_refused_before_loading(self):
        injections = {
            "gravcomp": lambda root: body_named(root, "base_link").set("gravcomp", "1"),
            "armature": lambda root: next(iter(root.iter("joint"))).set("armature", "100"),
            "option flag": lambda root: ET.SubElement(
                next(iter(root.iter("option"))), "flag"
            ).set("gravity", "disable"),
            "equality": lambda root: ET.SubElement(root, "equality").append(
                ET.Element("joint", {"joint1": "shoulder", "polycoef": "0 0 0 0 0"})
            ),
            "default": lambda root: ET.SubElement(root, "default").append(ET.Element("joint", {"damping": "5"})),
            "include": lambda root: ET.SubElement(root, "include").set("file", "somewhere-else.xml"),
        }
        for label, editor in injections.items():
            with self.subTest(label=label):
                bundle = self.tamper(f"inject-{label.replace(' ', '-')}")
                rewrite_robot(bundle, editor)
                manifest_digest = reseal_manifest(bundle)
                saved = json.loads((bundle / "reports/quality.json").read_text())
                saved["manifest_sha256"] = manifest_digest
                (bundle / "reports/quality.json").write_text(
                    json.dumps(saved, sort_keys=True, separators=(",", ":"))
                )
                self.assert_gate_failed(bundle, "engine.load")

    def test_resealed_mesh_path_escape_is_refused_before_loading(self):
        bundle = self.tamper("mesh-escape")

        def editor(root):
            for mesh in root.find("asset").findall("mesh"):
                mesh.set("file", "../../outside.stl")
                break

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "engine.load")

    def test_resealed_scene_extension_is_refused(self):
        bundle = self.tamper("scene-default")
        path = bundle / "scene.xml"
        root = ET.fromstring(path.read_bytes())
        ET.SubElement(root, "default").append(ET.Element("joint", {"damping": "5"}))
        ET.indent(root, space="  ")
        path.write_bytes(f'<?xml version="1.0" encoding="UTF-8"?>\n{ET.tostring(root, encoding="unicode")}\n'.encode())
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "engine.load")

    def test_semantic_neutral_formatting_remains_acceptable(self):
        bundle = self.tamper("reformat")
        path = bundle / "robot.xml"
        root = ET.fromstring(path.read_bytes())
        root.append(ET.Comment("reformatted, semantics unchanged"))
        ET.indent(root, space="    ")
        path.write_bytes(f'<?xml version="1.0" encoding="UTF-8"?>\n{ET.tostring(root, encoding="unicode")}\n'.encode())
        reseal_manifest(bundle)
        report = verifier.evaluate_bundle(bundle)
        self.assertTrue(report["passed"], report["checks"])

    def test_a_nan_field_is_refused_before_loading(self):
        bundle = self.tamper("nan")

        def editor(root):
            body_named(root, "base_link").find("inertial").set("mass", "nan")

        rewrite_robot(bundle, editor)
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "engine.load")

    def test_a_utf16_doctype_document_is_refused(self):
        bundle = self.tamper("utf16")
        path = bundle / "robot.xml"
        text = path.read_bytes().decode()
        text = text.replace("<mujoco", '<!DOCTYPE mujoco SYSTEM "elsewhere.dtd">\n<mujoco', 1)
        path.write_bytes(text.encode("utf-16"))
        reseal_manifest(bundle)
        self.assert_gate_failed(bundle, "engine.load")

    @unittest.skipIf(os.name == "nt", "symlinks are a POSIX fixture")
    def test_symlinked_delivered_file_fails_closure(self):
        bundle = self.tamper("symlink")
        target = bundle / "input/meshes/foot.stl"
        target.unlink()
        os.symlink("pad.stl", target)
        self.assert_gate_failed(bundle, "identity.closure")


class VertexSetTests(unittest.TestCase):
    def policy(self) -> dict:
        return {"tolerances": {"mesh_atol_m": 1e-6, "mesh_rtol": 1e-6}}

    def test_near_tie_sort_keys_still_match(self):
        expected = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.001], [1.0, 0.0, 0.0], [1.0, 0.0, 0.001]])
        engine = np.array(
            [[0.0, -1e-17, 0.001], [0.0, 1e-17, 0.0], [1.0, -1e-17, 0.001], [1.0, 1e-17, 0.0]]
        )
        residual = verifier._compare_vertex_sets(engine, expected, self.policy())
        self.assertLess(residual, 1e-6)

    def test_float32_collapsed_near_duplicates_still_match(self):
        expected = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 5e-7], [1.0, 0.0, 0.0]])
        engine = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        residual = verifier._compare_vertex_sets(engine, expected, self.policy())
        self.assertLess(residual, 1e-6)

    def test_a_missing_vertex_is_still_rejected(self):
        engine = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        expected = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
        with self.assertRaises(verifier.CheckError):
            verifier._compare_vertex_sets(engine, expected, self.policy())

    def test_a_displaced_vertex_is_still_rejected(self):
        engine = np.array([[0.0, 0.0, 0.0], [1.0, 0.01, 0.0]])
        expected = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        with self.assertRaises(verifier.CheckError):
            verifier._compare_vertex_sets(engine, expected, self.policy())


class NumericGateTests(unittest.TestCase):
    def test_non_finite_inputs_are_refused(self):
        with self.assertRaises(verifier.CheckError):
            verifier._close(float("nan"), 0.0, 1e-6)
        with self.assertRaises(verifier.CheckError):
            verifier._close([0.0, float("nan")], [0.0, 0.0], 1e-6)
        with self.assertRaises(verifier.CheckError):
            verifier._scaled_close(float("inf"), 1.0, 1e-9, 1e-12)
        with self.assertRaises(verifier.CheckError):
            verifier._scaled_close([], [1.0], 1e-9, 1e-12)

    def test_empty_comparisons_match_only_when_both_are_empty(self):
        self.assertEqual(verifier._close([], [], 1e-6), 0.0)
        self.assertEqual(verifier._scaled_close([], [], 1e-9, 1e-12), 0.0)


class CommitTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "renameat2 is a Linux syscall")
    def test_commit_moves_the_directory_without_replacing(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-commit-") as directory:
            base = Path(directory)
            staged = base / "staged"
            staged.mkdir()
            (staged / "file.txt").write_text("payload")
            output = base / "final"
            mjcf._commit_directory(staged, output)
            self.assertTrue((output / "file.txt").is_file())
            self.assertFalse(staged.exists())

    @unittest.skipIf(os.name == "nt", "renameat2 is a Linux syscall")
    def test_commit_refuses_an_existing_destination(self):
        for kind in ("directory", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="mjcf-commit-") as directory:
                base = Path(directory)
                staged = base / "staged"
                staged.mkdir()
                (staged / "file.txt").write_text("payload")
                output = base / "final"
                if kind == "directory":
                    output.mkdir()
                else:
                    output.write_text("already here")
                with self.assertRaises(PipelineError):
                    mjcf._commit_directory(staged, output)
                self.assertTrue((staged / "file.txt").is_file())
                self.assertTrue(output.exists())


class BuildStabilityTests(unittest.TestCase):
    def test_a_configuration_changed_mid_build_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-stability-") as directory:
            source = _Source(Path(directory))
            original = mjcf._project_robot

            def tampering_projection(document, config, sites):
                result = original(document, config, sites)
                source.config_path.write_text(json.dumps(configuration(base="fixed")))
                return result

            with (
                patch.object(mjcf, "_project_robot", side_effect=tampering_projection),
                self.assertRaises(PipelineError) as caught,
            ):
                source.build()
            self.assertIn("simulation configuration changed", str(caught.exception))
            self.assertFalse((source.config_path.parent / "delivery").exists())

    def test_an_unstable_runtime_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-unstable-") as directory:
            source = _Source(
                Path(directory),
                config=configuration(
                    simulation={"timestep": 0.1, "gravity": [0.0, 0.0, -1e12], "integrator": "implicitfast"},
                    support_sites=[],
                    floor={"height": -1.0, "friction": [0.9, 0.005, 0.0001]},
                ),
            )
            with self.assertRaises(PipelineError) as caught:
                source.build()
            self.assertIn("runtime.steps", str(caught.exception))
            self.assertFalse((source.config_path.parent / "delivery").exists())


class EmittedAssetTests(unittest.TestCase):
    def test_a_collision_only_mesh_is_sealed_but_not_registered_for_visual_convex(self):
        for source in ("visual_convex", "urdf"):
            with self.subTest(source=source), tempfile.TemporaryDirectory(prefix="mjcf-assets-") as directory:
                source_tree = _Source(
                    Path(directory),
                    config=configuration(
                        collision={"source": source, "self_collision": True, "friction": [0.8, 0.005, 0.0001]},
                        support_sites=[],
                        floor={"height": -1.0, "friction": [0.9, 0.005, 0.0001]},
                    ),
                )
                bundle = source_tree.build()
                manifest = json.loads((bundle / "manifest.json").read_text())
                self.assertIn("input/meshes/collision_only.stl", manifest["files"])
                robot = ET.fromstring((bundle / "robot.xml").read_bytes())
                assets = {mesh.get("file") for mesh in robot.find("asset").findall("mesh")}
                if source == "urdf":
                    self.assertIn("input/meshes/collision_only.stl", assets)
                else:
                    self.assertNotIn("input/meshes/collision_only.stl", assets)
                report = verifier.check_bundle(bundle)
                self.assertTrue(report["passed"], report["checks"])


class SupportHullTests(unittest.TestCase):
    def test_a_distant_plane_contact_point_extends_the_support_hull(self):
        import math

        with tempfile.TemporaryDirectory(prefix="mjcf-hull-") as directory:
            source = _Source(Path(directory), meshes={"foot.stl": far_point_stl()})
            bundle = source.build()
            robot = ET.fromstring((bundle / "robot.xml").read_bytes())
            site = next(element for element in robot.iter("site") if element.get("name") == "left_sole")
            hull = np.array([[0.0, 0.0], [0.2, 0.0], [1.0, 0.05], [0.2, 0.1], [0.0, 0.1]])
            x, y = hull[:, 0], hull[:, 1]
            cross = x * np.roll(y, -1) - np.roll(x, -1) * y
            area = cross.sum() / 2
            centroid_x = ((x + np.roll(x, -1)) * cross).sum() / (6 * area)
            centroid_y = ((y + np.roll(y, -1)) * cross).sum() / (6 * area)
            tilt = 0.12
            expected = (centroid_x, centroid_y * math.cos(tilt), 0.05 + centroid_y * math.sin(tilt))
            actual = [float(value) for value in site.get("pos").split()]
            for axis in range(3):
                self.assertAlmostEqual(actual[axis], expected[axis], delta=1e-9)
            self.assertGreater(actual[0], 0.3, "the distant contact point must extend the hull")
            report = verifier.check_bundle(bundle)
            self.assertTrue(report["passed"], report["checks"])


class MinimalDeliveryTests(unittest.TestCase):
    def test_build_without_a_configuration_seals_the_empty_object(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-minimal-") as directory:
            source = _Source(Path(directory))
            bundle = source.build(config=False)
            self.assertEqual((bundle / "simulation.json").read_bytes(), b"{}\n")
            report = verifier.check_bundle(bundle)
            self.assertTrue(report["passed"], report["checks"])
            facts = report["facts"]
            self.assertEqual(facts["base"], "floating")
            self.assertEqual(facts["collision_source"], "urdf")
            self.assertFalse(facts["floor"])
            self.assertEqual(
                facts["effective_simulation"],
                {
                    "schema_version": 1,
                    "base": "floating",
                    "collision": {"source": "urdf", "self_collision": True, "friction": [0.8, 0.005, 0.0001]},
                    "simulation": {"timestep": 0.001, "gravity": [0.0, 0.0, -9.81], "integrator": "implicitfast"},
                    "sites": [],
                    "support_sites": [],
                },
            )
            self.assertEqual(
                sorted(path.name for path in bundle.iterdir()),
                ["input", "manifest.json", "reports", "robot.xml", "simulation.json"],
            )

    def test_an_explicit_empty_object_keeps_its_bytes(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-minimal-") as directory:
            source = _Source(Path(directory), config_bytes=b"{}")
            bundle = source.build()
            self.assertEqual((bundle / "simulation.json").read_bytes(), b"{}")
            self.assertTrue(verifier.check_bundle(bundle)["passed"])

    def test_partial_overrides_reach_the_scene_and_the_report(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-minimal-") as directory:
            config_bytes = json.dumps(
                {
                    "collision": {"friction": [0.9, 0.02, 0.002]},
                    "simulation": {"integrator": "Euler"},
                    "floor": {},
                }
            ).encode()
            source = _Source(Path(directory), config_bytes=config_bytes)
            bundle = source.build()
            report = verifier.check_bundle(bundle)
            self.assertTrue(report["passed"], report["checks"])
            effective = report["facts"]["effective_simulation"]
            self.assertEqual(
                effective["collision"], {"source": "urdf", "self_collision": True, "friction": [0.9, 0.02, 0.002]}
            )
            self.assertEqual(
                effective["simulation"],
                {"timestep": 0.001, "gravity": [0.0, 0.0, -9.81], "integrator": "Euler"},
            )
            self.assertEqual(effective["floor"], {"height": 0.0, "friction": [0.9, 0.02, 0.002]})
            scene = ET.fromstring((bundle / "scene.xml").read_bytes())
            floor = next(element for element in scene.iter("geom") if element.get("name") == contract.FLOOR_GEOM_NAME)
            self.assertEqual([float(value) for value in floor.get("friction").split()], [0.9, 0.02, 0.002])
            self.assertEqual(floor.get("pos").split(), ["0", "0", "0"])

    def test_floor_friction_override_wins_over_the_inherited_value(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-minimal-") as directory:
            config_bytes = json.dumps(
                {
                    "collision": {"friction": [0.9, 0.02, 0.002]},
                    "floor": {"friction": [0.7, 0.01, 0.001]},
                }
            ).encode()
            source = _Source(Path(directory), config_bytes=config_bytes)
            bundle = source.build()
            scene = ET.fromstring((bundle / "scene.xml").read_bytes())
            floor = next(element for element in scene.iter("geom") if element.get("name") == contract.FLOOR_GEOM_NAME)
            self.assertEqual([float(value) for value in floor.get("friction").split()], [0.7, 0.01, 0.001])

    def test_support_default_names_and_zero_coordinate_sites(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-minimal-") as directory:
            config_bytes = json.dumps(
                {
                    "sites": [{"name": "mark", "link": "tool_link"}],
                    "support_sites": [{"link": "base_link"}],
                    "floor": {},
                }
            ).encode()
            source = _Source(Path(directory), config_bytes=config_bytes)
            bundle = source.build()
            robot = ET.fromstring((bundle / "robot.xml").read_bytes())
            sites = {element.get("name"): element for element in robot.iter("site")}
            self.assertIn("base_link__support", sites)
            self.assertIsNone(sites["mark"].get("pos"))
            self.assertIsNone(sites["mark"].get("quat"))
            report = verifier.check_bundle(bundle)
            self.assertTrue(report["passed"], report["checks"])
            self.assertEqual(report["facts"]["support_sites"], 1)
            self.assertTrue(report["facts"]["floor"])

    def test_raw_configuration_bytes_are_sealed_verbatim(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-minimal-") as directory:
            config_bytes = b'{\n    "floor":    {  "height":  -0.5  }\n}\n'
            source = _Source(Path(directory), config_bytes=config_bytes)
            bundle = source.build()
            self.assertEqual((bundle / "simulation.json").read_bytes(), config_bytes)
            manifest = json.loads((bundle / "manifest.json").read_text())
            self.assertEqual(manifest["files"]["simulation.json"]["sha256"], hashlib.sha256(config_bytes).hexdigest())
            self.assertTrue(verifier.check_bundle(bundle)["passed"])


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT / "src")
        return subprocess.run(
            [sys.executable, "-B", "-m", "description_pipeline", *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_build_and_check_through_the_command_line(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-cli-") as directory:
            source = _Source(Path(directory))
            output = source.config_path.parent / "cli-delivery"
            built = self.run_cli(
                "mjcf", "build", str(source.root), "--urdf", "urdf/robot.urdf",
                "--config", str(source.config_path), "--output", str(output),
                cwd=REPO_ROOT,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            payload = json.loads(built.stdout)
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["output"], str(output))
            checked = self.run_cli("mjcf", "check", str(output), cwd=REPO_ROOT)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["passed"])
            (output / "stray.txt").write_text("x")
            failed = self.run_cli("mjcf", "check", str(output), cwd=REPO_ROOT)
            self.assertEqual(failed.returncode, 1)
            self.assertFalse(json.loads(failed.stdout)["passed"])
            refused = self.run_cli(
                "mjcf", "build", str(source.root), "--urdf", "urdf/robot.urdf",
                "--config", str(source.config_path), "--output", str(output),
                cwd=REPO_ROOT,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertFalse(json.loads(refused.stderr)["passed"])

    def test_build_without_a_configuration_via_the_command_line(self):
        with tempfile.TemporaryDirectory(prefix="mjcf-cli-default-") as directory:
            source = _Source(Path(directory))
            output = source.config_path.parent / "cli-default"
            built = self.run_cli(
                "mjcf", "build", str(source.root), "--urdf", "urdf/robot.urdf",
                "--output", str(output),
                cwd=REPO_ROOT,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue(json.loads(built.stdout)["passed"])
            self.assertEqual((output / "simulation.json").read_bytes(), b"{}\n")
            checked = self.run_cli("mjcf", "check", str(output), cwd=REPO_ROOT)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["passed"])
            missing = self.run_cli(
                "mjcf", "build", str(source.root), "--urdf", "urdf/robot.urdf",
                "--config", str(source.config_path.parent / "missing.json"),
                "--output", str(source.config_path.parent / "cli-missing"),
                cwd=REPO_ROOT,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("is not a file", missing.stderr)
            self.assertFalse((source.config_path.parent / "cli-missing").exists())


class IsolationTests(unittest.TestCase):
    def valid_report(self) -> dict:
        return {
            "schema_version": contract.REPORT_SCHEMA,
            "scope": contract.REPORT_SCOPE,
            "manifest_sha256": None,
            "facts": {},
            "checks": [
                {"id": identifier, "state": "passed", "passed": True, "details": {}}
                for identifier in contract.CHECK_IDS
            ],
            "passed": True,
        }

    def test_the_child_runs_with_isolated_interpreter_flag(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.valid_report()), "")
        with patch.object(verifier.subprocess, "run", return_value=completed) as run:
            report = verifier._run_child(Path("/nonexistent-bundle"))
        self.assertTrue(report["passed"])
        command = run.call_args.args[0]
        self.assertIn("-I", command)
        self.assertTrue(command[-1].endswith("verification/mjcf.py") or command[-1].endswith("verification\\mjcf.py"))
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["root"], "/nonexistent-bundle")
        self.assertEqual(request["policy"]["check_ids"], list(contract.CHECK_IDS))

    def test_a_failing_child_process_produces_a_failed_report(self):
        completed = subprocess.CompletedProcess([], 1, json.dumps({"error": "ImportError: no engine"}), "")
        with patch.object(verifier.subprocess, "run", return_value=completed):
            with self.assertRaises(verifier.CheckError):
                verifier._run_child(Path("/nonexistent-bundle"))
            broken = verifier.evaluate_bundle(Path("/nonexistent-bundle"))
        self.assertFalse(broken["passed"])
        self.assertEqual(broken["checks"][0]["id"], "engine.process")

    def test_the_verification_module_never_imports_the_converter(self):
        probe = (
            "import sys; import description_pipeline.verification.mjcf; "
            "assert 'description_pipeline.mjcf' not in sys.modules"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT / "src")
        completed = subprocess.run(
            [sys.executable, "-B", "-c", probe], cwd=REPO_ROOT, env=environment, capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
