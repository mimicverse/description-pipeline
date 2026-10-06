from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks.revision import check_successor, read_revision, seal_revision


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "assembly.SLDASM").write_bytes(b"assembly fixture bytes")
        (self.root / "part.SLDPRT").write_bytes(b"part fixture bytes")
        self.options = {
            "hardware_id": "arm",
            "revision": "r1",
            "owner": "mechanical",
            "system": "handoff",
            "reference": "mechanical/arm/r1",
            "summary": "Initial mechanical handoff",
        }

    def test_sealing_is_idempotent_and_preserves_cad_bytes(self):
        before = (self.root / "assembly.SLDASM").read_bytes()
        value = seal_revision(self.root, **self.options)
        self.assertEqual(value, seal_revision(self.root, **self.options))
        self.assertEqual(value, read_revision(self.root, hardware_id="arm"))
        self.assertEqual(before, (self.root / "assembly.SLDASM").read_bytes())

    def test_changed_or_added_or_removed_cad_is_rejected(self):
        seal_revision(self.root, **self.options)
        for action in ("change", "add", "remove"):
            with self.subTest(action=action):
                part = self.root / "part.SLDPRT"
                original = part.read_bytes()
                if action == "change":
                    part.write_bytes(b"changed")
                elif action == "add":
                    (self.root / "new.SLDPRT").write_bytes(b"new")
                else:
                    part.unlink()
                with self.assertRaises(PipelineError):
                    read_revision(self.root)
                part.write_bytes(original)
                (self.root / "new.SLDPRT").unlink(missing_ok=True)

    def test_existing_handoff_cannot_be_overwritten(self):
        seal_revision(self.root, **self.options)
        with self.assertRaises(PipelineError):
            seal_revision(self.root, **{**self.options, "revision": "r2", "parent_revision": "r1"})

    def test_hardware_and_parent_and_git_reference(self):
        seal_revision(self.root, **self.options)
        with self.assertRaises(PipelineError):
            read_revision(self.root, hardware_id="other")
        for options in ({"parent_revision": "r1"}, {"system": "git", "reference": "main"}):
            with self.subTest(options=options), self.assertRaises(PipelineError):
                seal_revision(self.root, **{**self.options, **options})

    def test_transient_locks_do_not_change_revision(self):
        value = seal_revision(self.root, **self.options)
        (self.root / "~$assembly.SLDASM").write_bytes(b"editor lock")
        self.assertEqual(value, read_revision(self.root))

    def test_resealed_same_id_cannot_publish_changed_content(self):
        previous = seal_revision(self.root, **self.options)
        current = json.loads(json.dumps(previous))
        current["cad_files"]["part.SLDPRT"] = "0" * 64
        with self.assertRaises(PipelineError):
            check_successor(previous, current)
        current["revision"] = "r2"
        with self.assertRaises(PipelineError):
            check_successor(previous, current)
        current["parent_revision"] = "r1"
        check_successor(previous, current)

    def test_missing_manifest_and_path_aliases_are_rejected(self):
        with self.assertRaises(PipelineError):
            read_revision(self.root)
        seal_revision(self.root, **self.options)
        (self.root / "alias.SLDPRT").symlink_to(self.root / "part.SLDPRT")
        with self.assertRaises(PipelineError):
            read_revision(self.root)

    def test_malformed_revision_metadata_has_a_controlled_failure(self):
        value = seal_revision(self.root, **self.options)
        for field, bad in (
            ("control", {"system": [], "reference": "source"}),
            ("owner", {}),
            ("revision", "../outside"),
            ("cad_files", []),
        ):
            with self.subTest(field=field):
                (self.root / "cad-revision.json").write_text(json.dumps({**value, field: bad}))
                with self.assertRaises(PipelineError):
                    read_revision(self.root)


if __name__ == "__main__":
    unittest.main()
