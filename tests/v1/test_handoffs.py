"""Native handoff observation controls; synthetic CAD is never qualified."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, digest
from description_pipeline.sources.solidworks.handoff import describe_handoff
from description_pipeline.sources.solidworks.revision import package_inventory


def make_handoff(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "总装.SLDASM").write_bytes(b"Non-native input control fixture")
    (root / "零件.SLDPRT").write_bytes(b"Non-native part control fixture")
    return root


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = make_handoff(self.root / "结构组 新版本")

    def test_native_binding_rejects_generated_inputs_and_empty_assemblies(self) -> None:
        identity = describe_handoff(self.source)
        self.assertEqual(digest(package_inventory(self.source)), identity["handoff_sha256"])
        self.assertNotIn("kind", identity)
        self.assertNotIn("hardware_id", identity)
        self.assertNotIn("revision_sha256", identity)
        for name in ("robot.yaml", "cad-revision.json"):
            with self.subTest(name=name):
                path = self.source / name
                path.write_text("not a native engineering file")
                with self.assertRaises(PipelineError):
                    describe_handoff(self.source)
                path.unlink()
        (self.source / "总装.SLDASM").write_bytes(b"")
        with self.assertRaises(PipelineError):
            describe_handoff(self.source)

    def test_linked_source_parent_is_rejected(self) -> None:
        alias = self.root / "linked-parent"
        alias.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(PipelineError):
            describe_handoff(alias / self.source.name)


if __name__ == "__main__":
    unittest.main()
