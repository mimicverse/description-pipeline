"""Adversarial archive paths and changed release assets must fail before installation."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
import re
import shutil
import sys
from pathlib import Path

from description_pipeline.io import PipelineError, inventory, write_json
from tools.verify_distribution import safe_members, verify_release


class DistributionTests(unittest.TestCase):
    def test_windows_paths_and_case_collisions_are_rejected_on_linux(self):
        for names in (["../outside"], ["..\\..\\outside"], ["C:\\outside"], ["/outside"], ["a.py", "A.py"]):
            with self.subTest(names=names), self.assertRaises(PipelineError):
                safe_members(names)

    def test_release_checks_every_asset_and_its_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"source_commit": "a" * 40}
            path = root / "deployment.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("docs/mechanical-handoff-spec.md", "approved\n")
            write_json(root / "release.json", {**identity, "artifacts": inventory(root)})
            verify_release(root, identity)
            with self.assertRaises(PipelineError):
                verify_release(root, {"source_commit": "b" * 40})
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("docs/unknown.md", "extra\n")
            with self.assertRaises(PipelineError):
                verify_release(root, identity)

    def test_legacy_second_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"source_commit": "a" * 40}
            (root / "artifact.zip").write_bytes(b"release bytes")
            write_json(root / "release.json", {**identity, "artifacts": inventory(root)})
            write_json(root / "SHA256SUMS.json", {})
            with self.assertRaises(PipelineError) as raised:
                verify_release(root, identity)
            self.assertIn("SHA256SUMS.json", str(raised.exception))

    def test_missing_and_tampered_artifacts_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"source_commit": "a" * 40}
            artifact = root / "artifact.zip"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("member.txt", "original\n")
            write_json(root / "release.json", {**identity, "artifacts": inventory(root)})
            verify_release(root, identity)
            with zipfile.ZipFile(artifact, "a") as archive:
                archive.writestr("appended.txt", "tampered\n")
            with self.assertRaises(PipelineError):
                verify_release(root, identity)
            artifact.unlink()
            with self.assertRaises(PipelineError):
                verify_release(root, identity)

    def test_builder_binds_the_specified_source_closure(self):
        from tools import build_release

        root = Path(__file__).resolve().parents[2]
        version = re.search(
            r'__version__ = "([^"]+)"', (root / "src/description_pipeline/__init__.py").read_text()
        ).group(1)
        with tempfile.TemporaryDirectory() as directory:
            decoy = Path(directory) / "ambient"
            shutil.copytree(root / "src/description_pipeline", decoy / "description_pipeline")
            init = decoy / "description_pipeline/__init__.py"
            init.write_text(init.read_text().replace(f'__version__ = "{version}"', '__version__ = "0.0.0"'))
            saved = {
                name: module
                for name, module in sys.modules.items()
                if name == "description_pipeline" or name.startswith("description_pipeline.")
            }
            sys.path.insert(0, str(decoy))
            try:
                import importlib

                for name in [
                    name
                    for name in list(sys.modules)
                    if name == "description_pipeline" or name.startswith("description_pipeline.")
                ]:
                    del sys.modules[name]
                shadow = importlib.import_module("description_pipeline")
                self.assertEqual(shadow.__version__, "0.0.0")
                bound = build_release.bind_source_closure(root / "src")
                self.assertEqual(bound.version, version)
            finally:
                sys.path.remove(str(decoy))
                for name in [
                    name
                    for name in list(sys.modules)
                    if name == "description_pipeline" or name.startswith("description_pipeline.")
                ]:
                    del sys.modules[name]
                sys.modules.update(saved)

    def test_build_refuses_a_missing_runtime_or_unlocked_builders(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from tools import build_release

        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            with (
                patch.object(
                    build_release,
                    "bind_source_closure",
                    return_value=SimpleNamespace(PipelineError=PipelineError),
                ),
                patch.object(build_release, "sys", SimpleNamespace(version_info=(3, 11, 9))),
                self.assertRaisesRegex(PipelineError, "CPython 3.12"),
            ):
                build_release.build(root, output)
            with (
                patch.object(
                    build_release,
                    "bind_source_closure",
                    return_value=SimpleNamespace(PipelineError=PipelineError),
                ),
                patch.object(build_release, "git", return_value=""),
                patch.object(build_release.importlib.metadata, "version", return_value="0.0.0"),
                self.assertRaisesRegex(PipelineError, "Install requirements/build-py312.lock"),
            ):
                build_release.build(root, output)

    def test_bytecode_caches_are_not_part_of_the_source_closure(self):
        from types import SimpleNamespace

        from tools import build_release

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "module.py").write_text("value = 1\n", encoding="utf-8")
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"bytecode")
            self.assertEqual(
                build_release.source_inventory(package, SimpleNamespace(inventory=inventory)),
                {"module.py": hashlib.sha256(b"value = 1\n").hexdigest()},
            )
