"""Public behavior, adversarial re-sealing, and portable physical-model delivery."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from description_pipeline.io import PipelineError, digest, write_json


def cube_stl(path: Path) -> None:
    vertices = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
    faces = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    raw = b"synthetic cube".ljust(80, b"\0") + struct.pack("<I", len(faces))
    for face in faces:
        points = [coordinate * 0.07 for index in face for coordinate in vertices[index]]
        raw += struct.pack("<12fH", 0, 0, 0, *points, 0)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)


def inertial(mass: float) -> str:
    return (
        f'<inertial><origin xyz="0.0123456789123 -0.0234567891234 0.0345678912345" '
        f'rpy="0.123456789123 -0.234567891234 0.345678912345"/><mass value="{mass}"/>'
        '<inertia ixx="0.2" ixy="0.01123456789123" ixz="-0.02123456789123" '
        'iyy="0.3" iyz="0.03123456789123" izz="0.4"/></inertial>'
    )


def fixture(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    (source / "urdf").mkdir(parents=True)
    cube_stl(source / "meshes/cube.stl")
    mesh = (
        '<origin xyz="0.03 0.01 -0.02" rpy="0.3 -0.2 0.1"/><geometry>'
        '<mesh filename="../meshes/cube.stl" scale="1.2 0.8 1.5"/></geometry>'
    )
    urdf = (
        f'<robot name="neutral_fixture"><link name="base">{inertial(2.123456789123)}'
        '<visual><geometry><box size="0.2 0.3 0.4"/></geometry></visual>'
        '<collision><geometry><box size="0.2 0.3 0.4"/></geometry></collision></link>'
        f'<link name="arm">{inertial(1.234567891234)}<visual>{mesh}'
        '<material name="blue"><color rgba="0.1 0.2 0.9 1"/></material></visual>'
        f"<collision>{mesh}</collision></link>"
        f'<link name="slider">{inertial(0.5)}<visual>{mesh}</visual>'
        '<collision><geometry><sphere radius="0.08"/></geometry></collision></link>'
        f'<link name="spinner">{inertial(0.25)}'
        '<visual><geometry><cylinder radius="0.02" length="0.1"/></geometry></visual></link>'
        '<link name="tool_frame"/>'
        '<joint name="hinge" type="revolute"><parent link="base"/><child link="arm"/>'
        '<origin xyz="0.123456789123 -0.2 0.3" rpy="0.2 -0.3 0.4"/>'
        '<axis xyz="0 0.6 0.8"/><limit lower="-0.37" upper="1.23" effort="0" velocity="0"/>'
        '<dynamics damping="0.0123456789123" friction="0.0234567891234"/></joint>'
        '<joint name="slide" type="prismatic"><parent link="arm"/><child link="slider"/>'
        '<origin xyz="0.2 0 0.1" rpy="0.1 0.2 -0.1"/><axis xyz="1 0 0"/>'
        '<limit lower="-0.1" upper="0.3" effort="10" velocity="2"/></joint>'
        '<joint name="spin" type="continuous"><parent link="slider"/><child link="spinner"/>'
        '<origin xyz="0 0.1 0"/><axis xyz="0 1 0"/></joint>'
        '<joint name="tool_fixed" type="fixed"><parent link="spinner"/><child link="tool_frame"/>'
        '<origin xyz="0.01 0.02 0.03" rpy="0.1 0.2 0.3"/></joint></robot>'
    )
    (source / "urdf/robot.urdf").write_text(urdf)
    config = root / "simulation.json"
    write_json(
        config,
        {
            "schema_version": 1,
            "base": "floating",
            "collision": {"source": "urdf", "self_collision": True, "friction": [0.8, 0.005, 0.0001]},
            "simulation": {"timestep": 0.001, "gravity": [0, 0, -9.81], "integrator": "implicitfast"},
            "sites": [{"name": "tool", "link": "tool_frame", "xyz": [0.01, 0.02, 0.03], "rpy": [0.2, 0.3, 0.4]}],
            "floor": {"height": -2, "friction": [0.7, 0.004, 0.0002]},
        },
    )
    return source, config


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.config = fixture(self.root)
        self.output = self.root / "delivery"

    def build(self):
        from description_pipeline.mjcf import build

        return build(self.source, Path("urdf/robot.urdf"), self.config, self.output)

    def check(self, *, compare_saved=True):
        from description_pipeline.verification.mjcf import check_bundle

        return check_bundle(self.output, compare_saved=compare_saved)

    def config_update(self, **changes):
        data = json.loads(self.config.read_text())
        data.update(changes)
        write_json(self.config, data)

    def reseal(self):
        from description_pipeline.mjcf_contract import inventory

        manifest = json.loads((self.output / "manifest.json").read_text())
        manifest["files"] = inventory(self.output)
        manifest.pop("subject_sha256")
        manifest["subject_sha256"] = digest(manifest)
        write_json(self.output / "manifest.json", manifest)

    def alter_xml(self, edit, filename="robot.xml"):
        path = self.output / filename
        tree = ET.parse(path)
        edit(tree.getroot())
        tree.write(path, encoding="utf-8", xml_declaration=True)
        self.reseal()

    def test_full_precision_physics_and_portability(self):
        import mujoco
        import numpy as np

        original = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        self.assertTrue(self.build()["passed"])
        model = mujoco.MjModel.from_xml_path(str(self.output / "robot.xml"))
        self.assertEqual((model.nq, model.nv, model.nu), (10, 9, 0))
        self.assertEqual(model.nbody, 6)
        self.assertAlmostEqual(model.body("base").mass[0], 2.123456789123, places=12)
        self.assertAlmostEqual(model.body("arm").mass[0], 1.234567891234, places=12)
        self.assertTrue(np.isfinite(model.body_inertia).all())
        self.assertTrue(self.check()["passed"])
        self.assertEqual(
            original, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        )
        moved = self.root / "relocated"
        shutil.copytree(self.output, moved)
        shutil.rmtree(self.source)
        self.config.unlink()
        from description_pipeline.verification.mjcf import check_bundle

        self.assertTrue(check_bundle(moved)["passed"])

    def test_two_roots_produce_identical_bytes(self):
        self.build()
        other_root = self.root / "elsewhere"
        other_source, other_config = fixture(other_root)
        from description_pipeline.mjcf import build

        other_output = other_root / "out"
        self.assertTrue(build(other_source, Path("urdf/robot.urdf"), other_config, other_output)["passed"])
        first = {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        second = {p.relative_to(other_output): p.read_bytes() for p in other_output.rglob("*") if p.is_file()}
        self.assertEqual(first, second)

    def test_fixed_base_and_no_scene(self):
        import mujoco

        config = json.loads(self.config.read_text())
        config["base"] = "fixed"
        config.pop("floor")
        write_json(self.config, config)
        self.assertTrue(self.build()["passed"])
        model = mujoco.MjModel.from_xml_path(str(self.output / "robot.xml"))
        self.assertEqual((model.nq, model.nv), (3, 3))
        self.assertFalse((self.output / "scene.xml").exists())

    def test_all_collision_policies_are_explicit(self):
        import mujoco

        for index, mode in enumerate(("urdf", "visual_convex", "none")):
            with self.subTest(mode=mode):
                self.output = self.root / f"delivery-{index}"
                self.config_update(
                    collision={"source": mode, "self_collision": False, "friction": [0.8, 0.005, 0.0001]}
                )
                self.assertTrue(self.build()["passed"])
                model = mujoco.MjModel.from_xml_path(str(self.output / "robot.xml"))
                self.assertEqual(sum(model.geom_contype != 0), {"urdf": 3, "visual_convex": 4, "none": 0}[mode])

    def test_no_overwrite(self):
        self.output.mkdir()
        (self.output / "keep").write_text("original")
        with self.assertRaises((PipelineError, OSError)):
            self.build()
        self.assertEqual((self.output / "keep").read_text(), "original")

    def test_resealed_mass_axis_and_site_tampering_are_rejected(self):
        edits = [
            lambda root: root.find(".//body[@name='arm']/inertial").set("mass", "1.3"),
            lambda root: root.find(".//joint[@name='hinge']").set("axis", "0 -0.6 -0.8"),
            lambda root: root.find(".//site[@name='tool']").set("pos", "0 0 0"),
        ]
        for index, edit in enumerate(edits):
            with self.subTest(index=index):
                self.output = self.root / f"tamper-{index}"
                self.build()
                self.alter_xml(edit)
                with self.assertRaises(PipelineError):
                    self.check(compare_saved=False)

    def test_resealed_scene_tampering_is_rejected(self):
        self.build()
        self.alter_xml(lambda root: root.find(".//geom[@type='plane']").set("pos", "0 0 3"), "scene.xml")
        with self.assertRaises(PipelineError):
            self.check(compare_saved=False)

    def test_extra_file_and_report_tampering_are_rejected(self):
        self.build()
        (self.output / "unexpected.xml").write_text("<mujoco/>")
        with self.assertRaises(PipelineError):
            self.check()
        (self.output / "unexpected.xml").unlink()
        report_path = self.output / "reports/quality.json"
        report = json.loads(report_path.read_text())
        report["passed"] = False
        write_json(report_path, report)
        with self.assertRaises(PipelineError):
            self.check()

    def test_unknown_config_and_duplicate_json_keys_fail(self):
        self.config_update(actuators={"hinge": {"kp": 100}})
        with self.assertRaises(PipelineError):
            self.build()
        text = self.config.read_text().replace('"base": "floating"', '"base": "fixed", "base": "floating"')
        self.config.write_text(text)
        with self.assertRaises(PipelineError):
            self.build()

    def test_unsupported_mechanism_and_invalid_inertia_fail(self):
        path = self.source / "urdf/robot.urdf"
        original = path.read_text()
        path.write_text(original.replace('<axis xyz="0 0.6 0.8"/>', '<axis xyz="0 0.6 0.8"/><mimic joint="spin"/>'))
        with self.assertRaises(PipelineError):
            self.build()
        path.write_text(original.replace('ixx="0.2"', 'ixx="-0.2"'))
        with self.assertRaises((PipelineError, ValueError)):
            self.build()

    def test_remote_mesh_and_symlink_fail(self):
        path = self.source / "urdf/robot.urdf"
        original = path.read_text()
        path.write_text(original.replace("../meshes/cube.stl", "https://example.com/cube.stl"))
        with self.assertRaises(PipelineError):
            self.build()
        path.write_text(original)
        mesh = self.source / "meshes/cube.stl"
        outside = self.root / "outside.stl"
        mesh.rename(outside)
        mesh.symlink_to(outside)
        with self.assertRaises(PipelineError):
            self.build()

    def test_cli_uses_the_same_complete_check(self):
        from description_pipeline.cli import main

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "mjcf",
                    "build",
                    str(self.source),
                    "--urdf",
                    "urdf/robot.urdf",
                    "--config",
                    str(self.config),
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["passed"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["mjcf", "check", str(self.output)]), 0)


if __name__ == "__main__":
    unittest.main()
