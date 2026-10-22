from __future__ import annotations

import math
import struct
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from description_pipeline.backends.urdf import MESH_PART_TRIANGLE_LIMIT, generate_urdf
from description_pipeline.io import PipelineError, file_digest, read_data
from description_pipeline.model import Robot
from description_pipeline.sources.solidworks.naming import material_name, mesh_path
from description_pipeline.verification.solidworks_urdf import _xml


def _binary_stl(triangles: int) -> bytes:
    """Deterministic exact binary STL whose records are distinct per index."""
    records = b"".join(
        struct.pack(
            "<12fH",
            0.0, 0.0, 1.0,
            float(index), 0.0, 0.0,
            float(index), 1.0, 0.0,
            float(index), 0.0, 1.0,
            index % 65536,
        )
        for index in range(triangles)
    )
    return bytes(range(80)) + struct.pack("<I", triangles) + records


def _mesh_scene(filename: str) -> dict:
    """Fixture scene with base_link's first visual and collision repointed."""
    source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
    scene = read_data(source / "scene.json")
    base = next(link for link in scene["links"] if link["name"] == "base_link")
    for role in ("visuals", "collisions"):
        base[role][0]["filename"] = filename
    return scene


def _floats(text: str) -> list[float]:
    return [float(value) for value in text.split()]


def _staged_source(root: Path, raw: bytes, name: str = "geometry/big.stl") -> Path:
    """Temp source root carrying the fixture geometry plus one oversized mesh."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
    source = root / "source"
    shutil.copytree(fixture / "geometry", source / "geometry")
    (source / name).write_bytes(raw)
    return source


class UrdfNumericContractTests(unittest.TestCase):
    """R17 owner contract: the real writer emits io.format_number tokens on its artifacts."""

    def test_real_artifact_uses_the_single_numeric_rule(self) -> None:
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        joint = next(row for row in scene["joints"] if row["type"] == "revolute")
        joint["xyz"] = [0.1, math.pi, 1e-12]
        joint["rpy"] = [-0.0, 2.5, 0.0]
        joint["limits"] = {"lower": 0.1, "upper": math.pi, "effort": 1e-12, "velocity": 2.5}
        robot = Robot.from_dict(scene)
        with tempfile.TemporaryDirectory() as temp:
            output = generate_urdf(robot, source, Path(temp), name="numeric_contract")
            node = next(row for row in ET.parse(output).getroot().findall("joint") if row.get("name") == joint["name"])
            xyz = node.find("origin").attrib["xyz"].split()
            rpy = node.find("origin").attrib["rpy"].split()
            limits = node.find("limit").attrib
        # Shortest binary64 round-trip tokens, produced by the one numeric owner.
        self.assertEqual(xyz, ["0.1", "3.141592653589793", "1e-12"])
        self.assertEqual(rpy, ["0", "2.5", "0"])  # -0.0 renders the unified zero
        self.assertEqual(
            limits,
            {"lower": "0.1", "upper": "3.141592653589793", "effort": "1e-12", "velocity": "2.5"},
        )
        for token, value in zip(xyz + rpy, list(joint["xyz"]) + list(joint["rpy"]), strict=True):
            self.assertEqual(float(token), value)


class UrdfWriterTests(unittest.TestCase):
    def test_broken_joint_reference_is_rejected_before_writing(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        scene["joints"][0]["child"] = "missing_link"
        with self.assertRaisesRegex(PipelineError, "Unknown joint endpoint"):
            Robot.from_dict(scene)

    def test_diagnostic_urdf_preserves_declared_zero_limits(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        joint = next(row for row in scene["joints"] if row["type"] == "revolute")
        joint["limits"] = {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0}
        robot = Robot.from_dict(scene)
        with tempfile.TemporaryDirectory() as temp:
            output = generate_urdf(robot, source, Path(temp), name="diagnostic_arm")
            node = next(row for row in ET.parse(output).getroot().findall("joint") if row.get("name") == joint["name"])
            self.assertEqual(
                {key: float(value) for key, value in node.find("limit").attrib.items()},
                joint["limits"],
            )

    def test_engineering_default_policy_and_warning_reach_the_urdf(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        joint = next(row for row in scene["joints"] if row["type"] == "revolute")
        joint["limit_source"] = "default_policy"
        joint["limits"] = {"lower": -math.pi, "upper": math.pi, "effort": 1.0, "velocity": 1.0}
        joint["provenance"]["default_policy"] = {"rule_id": "engineering_rotation_default_window/v1"}
        scene["provenance"].setdefault("value_warnings", []).append(
            {
                "code": "limits.default_policy",
                "message": (
                    "Engineering uses a versioned default rotation window because no usable native "
                    "or authored position range was read; the engineering position range is unqualified."
                ),
                "objects": [{"object": joint["name"], "field": "limits"}],
            }
        )
        robot = Robot.from_dict(scene)
        with tempfile.TemporaryDirectory() as temp:
            output = generate_urdf(robot, source, Path(temp), name="default_policy_arm")
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            document = ET.fromstring(output.read_bytes(), parser=parser)
        node = next(row for row in document.findall("joint") if row.get("name") == joint["name"])
        self.assertEqual(
            {key: float(value) for key, value in node.find("limit").attrib.items()},
            joint["limits"],
        )
        comments = [child.text or "" for child in document if child.tag is ET.Comment]
        self.assertTrue(any("WARNING limits.default_policy:" in text for text in comments))
        self.assertTrue(any(
            f'"object": "{joint["name"]}"' in text and '"field": "limits"' in text for text in comments
        ))

    def test_warning_comments_keep_every_object_and_escape_xml_terminators(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        scene["provenance"]["value_warnings"] = [{
            "code": "limits.default_policy",
            "message": "Non-native default -- unqualified",
            "objects": [{"object": name, "field": "limits", "native_cad_ranges": False}
                        for name in ("joint:left", "joint:right--special")],
        }]
        with tempfile.TemporaryDirectory() as temp:
            output = generate_urdf(Robot.from_dict(scene), source, Path(temp), name="warning_test")
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            document = ET.fromstring(output.read_bytes(), parser=parser)
        comments = [child.text or "" for child in document if child.tag is ET.Comment]
        self.assertEqual(len(comments), 2)
        self.assertIn('"object": "joint:left"', comments[0])
        self.assertIn(r'"object": "joint:right\u002d\u002dspecial"', comments[1])
        for text in comments:
            self.assertIn('"field": "limits"', text)
            self.assertIn('"native_cad_ranges": false', text)

    def test_independent_xml_gate_rejects_changed_warning_scope_or_qualification(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        scene["provenance"]["value_warnings"] = [{
            "code": "limits.default_policy", "message": "Unqualified default range",
            "objects": [{"object": name, "field": "limits", "hardware_qualified": False}
                        for name in ("joint:left", "joint:right")],
        }]
        robot = Robot.from_dict(scene)
        with tempfile.TemporaryDirectory() as temp:
            output = generate_urdf(robot, source, Path(temp), name="warning_test")
            original = output.read_bytes()
            definition = {"source": {"robot_name": "warning_test"}}
            _xml(Path(temp), definition, robot.to_dict())
            for old, new in (
                (b'"object": "joint:left"', b'"object": "joint:wrong"'),
                (b'"field": "limits"', b'"field": "mass"'),
                (b'"hardware_qualified": false', b'"hardware_qualified": true'),
            ):
                with self.subTest(change=new):
                    output.write_bytes(original.replace(old, new, 1))
                    with self.assertRaisesRegex(PipelineError, "value-provenance warnings differ"):
                        _xml(Path(temp), definition, robot.to_dict())
            start = original.index(b"<!--")
            end = original.index(b"-->", start) + 3
            output.write_bytes(original[:start] + original[end:])
            with self.assertRaisesRegex(PipelineError, "value-provenance warnings differ"):
                _xml(Path(temp), definition, robot.to_dict())

    def test_mesh_delivery_is_self_contained_and_deterministic(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        robot = Robot.from_dict(read_data(source / "scene.json"))
        original = robot.to_dict()
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "first", Path(temp) / "second"
            first.mkdir()
            second.mkdir()
            output = generate_urdf(robot, source, first, name="fixture_arm")
            other = generate_urdf(robot, source, second, name="fixture_arm")
            self.assertEqual(output.read_bytes(), other.read_bytes())
            document = ET.parse(output).getroot()
            self.assertEqual(document.get("name"), "fixture_arm")
            self.assertEqual(len(document.findall("link")), len(original["links"]) + len(original["frames"]))
            self.assertEqual(len(document.findall("joint")), len(original["joints"]) + len(original["frames"]))
            for mesh in document.findall(".//mesh"):
                path = (output.parent / mesh.attrib["filename"]).resolve()
                self.assertTrue(path.is_relative_to(first))
                self.assertTrue(path.is_file())
                self.assertEqual(file_digest(path), file_digest(second / path.relative_to(first)))
            self.assertEqual(robot.to_dict(), original)
            self.assertNotIn(b"\r\n", output.read_bytes())

    def test_mesh_at_part_limit_keeps_the_single_delivery_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = _binary_stl(MESH_PART_TRIANGLE_LIMIT)
            source = _staged_source(root, original)
            robot = Robot.from_dict(_mesh_scene("geometry/big.stl"))
            destination = root / "delivery"
            output = generate_urdf(robot, source, destination, name="fixture_arm")
            node = next(link for link in ET.parse(output).getroot().findall("link") if link.get("name") == "base_link")
            expected = "../" + mesh_path(file_digest(source / "geometry/big.stl"), namespace="fixture_arm")
            self.assertEqual(
                [visual.find("geometry/mesh").get("filename") for visual in node.findall("visual")],
                [expected],
            )
            self.assertEqual((destination / expected.removeprefix("../")).read_bytes(), original)
            self.assertEqual(list((destination / "meshes").rglob("*_part_*")), [])

    def test_oversized_mesh_is_partitioned_losslessly_and_deterministically(self):
        total = 362_415  # The native8 PCB sheet mesh that must travel as two chunks.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = _binary_stl(total)
            source = _staged_source(root, original)
            source_file = source / "geometry/big.stl"
            before = file_digest(source_file)
            robot = Robot.from_dict(_mesh_scene("geometry/big.stl"))
            canonical = robot.to_dict()
            first, second = root / "first", root / "second"
            output = generate_urdf(robot, source, first, name="fixture_arm")
            generate_urdf(robot, source, second, name="fixture_arm")
            links = {link.get("name"): link for link in ET.parse(output).getroot().findall("link")}
            base = links["base_link"]
            visuals = base.findall("visual")
            stem = mesh_path(file_digest(source_file), namespace="fixture_arm")[:-4]
            expected_names = [
                f"../{stem}_part_0.stl",
                f"../{stem}_part_1.stl",
            ]
            self.assertEqual([visual.find("geometry/mesh").get("filename") for visual in visuals], expected_names)
            self.assertEqual(len(base.findall("collision")), 2)
            self.assertFalse((first / "meshes/visual/base_link_0.stl").exists())
            expected_counts = [MESH_PART_TRIANGLE_LIMIT, total - MESH_PART_TRIANGLE_LIMIT]
            delivered = []
            for url, count in zip(expected_names, expected_counts, strict=True):
                relative = url.removeprefix("../")
                part = (first / relative).read_bytes()
                self.assertEqual(struct.unpack_from("<I", part, 80)[0], count)
                self.assertEqual(len(part), 84 + 50 * count)
                self.assertEqual(part[:80], original[:80])
                self.assertEqual(file_digest(first / relative), file_digest(second / relative))
                delivered.append(part)
            self.assertEqual(b"".join(part[84:] for part in delivered), original[84:])
            self.assertEqual([struct.unpack_from("<I", part, 80)[0] for part in delivered], expected_counts)
            for visual in visuals:
                self.assertEqual(_floats(visual.find("origin").get("xyz")), [0.0, 0.0, 0.03125])
                self.assertEqual(_floats(visual.find("origin").get("rpy")), [0.0, 0.0, 0.0])
                self.assertEqual(_floats(visual.find("geometry/mesh").get("scale")), [1.0, 1.0, 1.0])
                material = visual.find("material")
                self.assertEqual(material.get("name"), material_name("geometry/big.stl", link="base_link"))
                self.assertEqual(_floats(material.find("color").get("rgba")), [0.7, 0.7, 0.72, 1.0])
            for collision in base.findall("collision"):
                self.assertEqual(_floats(collision.find("origin").get("xyz")), [0.0, 0.0, 0.03125])
                self.assertEqual(_floats(collision.find("geometry/mesh").get("scale")), [1.0, 1.0, 1.0])
                self.assertIsNone(collision.find("material"))
            self.assertEqual(file_digest(source_file), before)
            self.assertEqual(robot.to_dict(), canonical)
            self.assertEqual((first / "urdf/robot.urdf").read_bytes(), (second / "urdf/robot.urdf").read_bytes())

    def test_non_binary_stl_mesh_is_rejected(self):
        cases = {
            "ascii": b"solid fixture\nendsolid fixture\n",
            "truncated": _binary_stl(5)[:84],
            "trailing-bytes": _binary_stl(3) + b"\x00",
        }
        for label, raw in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = _staged_source(root, raw, "geometry/bad.stl")
                destination = root / "delivery"
                destination.mkdir()
                with self.assertRaises(PipelineError) as caught:
                    generate_urdf(
                        Robot.from_dict(_mesh_scene("geometry/bad.stl")), source, destination, name="fixture_arm"
                    )
                self.assertIn("binary STL", str(caught.exception))

    def test_primitive_visuals_keep_their_geometry_elements(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        base = next(link for link in scene["links"] if link["name"] == "base_link")
        base["visuals"] = [
            {
                "name": "base_link_box_visual",
                "kind": "box",
                "xyz": [0.0, 0.0, 0.0],
                "rpy": [0.0, 0.0, 0.0],
                "size": [1.0, 2.0, 3.0],
            },
            {
                "name": "base_link_sphere_visual",
                "kind": "sphere",
                "xyz": [0.0, 0.0, 0.0],
                "rpy": [0.0, 0.0, 0.0],
                "radius": 0.5,
            },
            {
                "name": "base_link_cylinder_visual",
                "kind": "cylinder",
                "xyz": [0.0, 0.0, 0.0],
                "rpy": [0.0, 0.0, 0.0],
                "radius": 0.25,
                "length": 0.75,
                "rgba": [0.1, 0.2, 0.3, 1.0],
                "material": "base_link_cylinder_material",
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            output = generate_urdf(Robot.from_dict(scene), source, Path(temp), name="fixture_arm")
            node = next(link for link in ET.parse(output).getroot().findall("link") if link.get("name") == "base_link")
            visuals = node.findall("visual")
            self.assertEqual(len(visuals), 3)
            self.assertEqual(_floats(visuals[0].find("geometry/box").get("size")), [1.0, 2.0, 3.0])
            self.assertEqual(_floats(visuals[1].find("geometry/sphere").get("radius")), [0.5])
            cylinder = visuals[2].find("geometry/cylinder")
            self.assertEqual(_floats(cylinder.get("radius")), [0.25])
            self.assertEqual(_floats(cylinder.get("length")), [0.75])
        self.assertEqual(visuals[2].find("material").get("name"), "base_link_cylinder_material")


if __name__ == "__main__":
    unittest.main()
