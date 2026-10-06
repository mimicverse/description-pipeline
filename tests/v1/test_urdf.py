from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from description_pipeline.backends.urdf import generate_urdf
from description_pipeline.io import file_digest, read_data
from description_pipeline.model import Robot


class UrdfWriterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
