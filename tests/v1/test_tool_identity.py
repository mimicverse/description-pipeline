"""Release integrity and optional-dependency closure are part of model provenance."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline import __version__, runtime, steps
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import PipelineError, digest, inventory, read_data, write_json


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

    @staticmethod
    def _publishable_bundle(root: Path) -> Path:
        """A minimal delivery whose inputs pass before the publication identity gate."""

        from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
        from description_pipeline.schemas import SOURCE_RECORD_SCHEMA

        bundle = root / "bundle"
        inputs = bundle / "input"
        inputs.mkdir(parents=True)
        (inputs / "arm.SLDASM").write_bytes(b"release-identity fixture\n")
        (inputs / "request.json").write_bytes(
            request_evidence_bytes(
                build_request_evidence(
                    purpose="engineering",
                    request_id="fixture-frozen-request",
                    target_hardware="arm",
                    change_summary="release identity fixture",
                    main_assembly="arm.SLDASM",
                    handoff_digest="a" * 64,
                    platform_config_digest="c" * 64,
                )
            )
        )
        identity = {
            "kind": "source-revision",
            "handoff_digest": digest({"fixture": "arm"}),
            "main_assembly": "arm.SLDASM",
            "configuration": "Default",
            "hardware_id": "arm",
            "records": [],
        }
        write_json(
            inputs / "source.json",
            {
                "schema_version": SOURCE_RECORD_SCHEMA,
                "source_revision_id": digest(identity),
                "identity": identity,
                "selection": {
                    "main_assembly": "arm.SLDASM",
                    "configuration": "Default",
                    "hardware_id": "arm",
                    "hardware_source": "cad",
                },
                "metadata": {
                    "revision": "arm-1",
                    "owner": "t",
                    "control": {"system": "handoff", "reference": "local-handoff"},
                    "change_summary": "release identity fixture",
                    "change_summary_source": "submission",
                },
            },
        )
        return bundle

    def test_publication_refuses_without_the_embedded_release_identity(self):
        """A source/PYTHONPATH install must refuse publication instead of degrading the gate (ORC-04)."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._publishable_bundle(root)
            calls: list = []
            with (
                patch.object(steps, "tool_record", return_value={"release": None}),
                patch(
                    "description_pipeline.repository.urdf_pr.submit_bundle",
                    side_effect=lambda *args, **kwargs: calls.append(args),
                ),
                self.assertRaisesRegex(PipelineError, "embedded release identity"),
            ):
                steps.publish_model(bundle, root / "repository")
            self.assertEqual(calls, [])
            self.assertFalse((bundle / "reports" / "pr.json").exists())

    def test_publication_proceeds_with_the_embedded_release_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._publishable_bundle(root)
            release = {"schema_version": "solidworks-to-urdf.release/v1", "version": __version__}
            submission = {"state": "published", "url": "https://github.com/example/arm/pull/1", "subject": "a" * 64}
            with (
                patch.object(steps, "tool_record", return_value={"release": release}),
                patch(
                    "description_pipeline.repository.urdf_pr.submit_bundle",
                    return_value=submission,
                ) as publish,
            ):
                result = steps.publish_model(bundle, root / "repository")
            publish.assert_called_once()
            self.assertEqual(publish.call_args.kwargs["branch"], "work/solidworks/arm/engineering")
            self.assertTrue(result["passed"])
            self.assertEqual(result["subject_sha256"], "a" * 64)
            self.assertEqual(read_data(bundle / "reports/pr.json")["url"], submission["url"])
