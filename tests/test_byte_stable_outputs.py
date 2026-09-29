"""Every delivered text artifact must be LF on every platform.

``Path.write_text`` and ``ElementTree.write`` open their target in text mode, so on Windows they
translate "\\n" into "\\r\\n".  The same model then carried different digests depending on where it
was built — visible today in this repository: ``feature/microban`` committed CRLF URDF/MJCF while
``release/microban`` committed LF.  These tests pin the invariant at the writer and at the source
level, because the rest of the pipeline is content-addressed.
"""

import ast
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from description_pipeline.backends import write_xml
from description_pipeline.sources.onshape.freeze import _write_json

ROOT = Path(__file__).resolve().parents[1]


class TextWriterTests(unittest.TestCase):
    def test_xml_writer_emits_lf_bytes(self):
        element = ET.Element("robot", name="robot")
        ET.SubElement(element, "link", name="base_link")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "robot.urdf"
            write_xml(path, element)
            data = path.read_bytes()

        self.assertTrue(data.startswith(b"<?xml"))
        self.assertNotIn(b"\r", data)
        self.assertIn(b"\n", data)

    def test_frozen_onshape_readings_are_lf(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "assembly_element.json"
            _write_json(path, {"id": "element", "values": [1, 2, 3]})
            data = path.read_bytes()

        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))

    def test_audit_report_is_lf(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "urdf_audit.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "audit.py"),
                    "--root",
                    str(ROOT / "examples" / "demo-arm"),
                    "--policy",
                    "strict",
                    "--report",
                    str(report),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
            data = report.read_bytes()

        self.assertNotIn(b"\r", data)

    def test_captured_output_is_utf8_even_under_another_locale(self):
        """Windows redirected the audit's Chinese summary through cp936; a UTF-8 reader failed."""

        import os
        import subprocess
        import sys

        environment = {**os.environ, "PYTHONIOENCODING": "cp936"}
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "audit.py"),
                "--root",
                str(ROOT / "examples" / "demo-arm"),
                "--policy",
                "strict",
            ],
            capture_output=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Decoding must not raise: the entry point pins UTF-8 regardless of the inherited locale.
        text = result.stdout.decode("utf-8")
        self.assertIn("error", text)


class SourceTextWriteTests(unittest.TestCase):
    """Any ``write_text`` in the package must pin ``newline`` explicitly."""

    def test_source_text_writes_pin_lf(self):
        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not isinstance(function, ast.Attribute) or function.attr != "write_text":
                    continue
                if "newline" not in {keyword.arg for keyword in node.keywords}:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], 'write_text must pin newline="\\n"; artifacts are byte-committed')


if __name__ == "__main__":
    unittest.main()
