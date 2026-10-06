"""Release integrity and optional-dependency closure are part of model provenance."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline import __version__, solidworks
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
            patch.object(solidworks, "RUNTIME_PACKAGES", ("reader",)),
            patch.object(solidworks.sys, "platform", "linux"),
            patch.object(solidworks.importlib.metadata, "distribution", distribution),
        ):
            self.assertEqual(solidworks.runtime_packages(), {"paths": "1.0", "reader": "1.0", "storage": "1.0"})

    def test_installed_identity_rejects_source_mutation_and_unlisted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            module = package / "solidworks.py"
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
                patch.object(solidworks, "__file__", str(module)),
                patch.object(solidworks, "runtime_packages", return_value={}),
            ):
                self.assertEqual(solidworks.tool_record()["release"], identity)
                module.write_text("changed\n", encoding="utf-8")
                with self.assertRaises(PipelineError):
                    solidworks.tool_record()
                module.write_text("source\n", encoding="utf-8")
                (package / "unlisted.py").write_text("unlisted\n", encoding="utf-8")
                with self.assertRaises(PipelineError):
                    solidworks.tool_record()
