"""Release integrity and optional-dependency closure are part of model provenance."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline import __version__, runtime
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import PipelineError, digest, inventory, write_json


class ToolIdentityTests(unittest.TestCase):
    def test_required_extras_and_nested_extras_are_recorded(self):
        rows = {
            "reader": ["paths[io]>=1"],
            "paths": ['storage>=1; extra == "io"', 'unused>=1; extra == "other"'],
            "storage": [],
        }

        def distribution(name):
            return SimpleNamespace(metadata={"Name": name}, version="1.0", requires=rows[name])

        with (
            patch.object(runtime, "required_packages", return_value=("reader",)),
            patch.object(runtime.sys, "platform", "linux"),
            patch.object(runtime.importlib.metadata, "distribution", distribution),
        ):
            self.assertEqual(runtime.runtime_packages(), {"paths": "1.0", "reader": "1.0", "storage": "1.0"})

    def test_installed_identity_rejects_source_mutation_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            module = package / "runtime.py"
            module.write_text("source\n", encoding="utf-8")
            files = inventory(package)
            identity = {
                "schema_version": "solidworks-to-urdf.release/v1",
                "pipeline_id": PIPELINE_ID,
                "version": __version__,
                "source_sha256": digest(files),
                "package_files": files,
            }
            write_json(package / "tool-release.json", identity)
            with (
                patch.object(runtime, "__file__", str(module)),
                patch.object(runtime, "runtime_packages", return_value={}),
            ):
                self.assertEqual(runtime.tool_record()["release"], identity)
                module.write_text("changed\n", encoding="utf-8")
                with self.assertRaises(PipelineError):
                    runtime.tool_record()
                module.write_text("source\n", encoding="utf-8")
                (package / "unlisted.py").write_text("unlisted\n", encoding="utf-8")
                with self.assertRaises(PipelineError):
                    runtime.tool_record()

    def test_inventory_rejects_directory_case_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Foo").mkdir()
            if (root / "foo").exists():
                self.skipTest("Directory aliases require a case-sensitive filesystem")
            (root / "Foo" / "a.txt").write_text("a\n", encoding="utf-8")
            (root / "foo").mkdir()
            (root / "foo" / "b.txt").write_text("b\n", encoding="utf-8")
            with self.assertRaises(PipelineError) as raised:
                inventory(root)
            self.assertIn("Case-colliding", str(raised.exception))
            (root / "foo" / "b.txt").unlink()
            (root / "foo").rmdir()
            (root / "Foo" / "nested").mkdir()
            (root / "Foo" / "nested" / "c.txt").write_text("c\n", encoding="utf-8")
            self.assertEqual(len(inventory(root)), 2)
