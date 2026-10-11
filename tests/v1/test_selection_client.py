"""Full-folder staging archive helper: exact index, no folder walks, bounded IO.

The helper is the Linux-side building block for folder staging: it archives
exactly the indexed names with identical hashes before, during and after
archiving; the native closure decides later which staged bytes are selected.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError, inventory
from description_pipeline.orchestration import selection_client
from description_pipeline.orchestration.selection_client import prepare_staging_archive


def make_tree(root: Path) -> None:
    (root / "deps").mkdir(parents=True)
    (root / "总装.SLDASM").write_bytes(b"assembly control fixture")
    (root / "零件.SLDPRT").write_bytes(b"part control fixture")
    (root / "deps" / "子零件.SLDPRT").write_bytes(b"nested part fixture")
    (root / "notes.txt").write_bytes(b"not a CAD dependency")
    (root / "decoy.SLDPRT").write_bytes(b"unrelated CAD decoy")


class SelectionClientTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "intake"
        make_tree(self.source)
        self.upload = inventory(self.source)
        self.archive = self.root / "staging.zip"

    def _sha(self, name: str) -> str:
        return hashlib.sha256((self.source / name).read_bytes()).hexdigest()

    def test_archives_the_full_indexed_folder(self) -> None:
        result = prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertEqual(result["files"], self.upload)
        self.assertEqual(result["bytes"], sum((self.source / name).stat().st_size for name in self.upload))
        with zipfile.ZipFile(self.archive) as sealed:
            self.assertEqual(sorted(sealed.namelist()), sorted(self.upload))
            for name, digest in self.upload.items():
                self.assertEqual(hashlib.sha256(sealed.read(name)).hexdigest(), digest)
            # Decoys are staged with the folder; the closure excludes them later.
            self.assertIn("decoy.SLDPRT", sealed.namelist())
            self.assertIn("notes.txt", sealed.namelist())

    def test_empty_index_is_rejected(self) -> None:
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, {})
        self.assertEqual(getattr(caught.exception, "code", ""), "selection_inventory_invalid")

    def test_oversized_and_overcount_folders_are_rejected(self) -> None:
        with patch.object(selection_client, "MAX_BATCH_FILES", 1), self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("file cap", str(caught.exception))
        with patch.object(selection_client, "MAX_BATCH_BYTES", 4), self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("byte cap", str(caught.exception))

    def test_changed_member_is_rejected_before_archiving(self) -> None:
        (self.source / "零件.SLDPRT").write_bytes(b"changed after upload")
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("changed before archiving", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_missing_member_is_rejected(self) -> None:
        (self.source / "零件.SLDPRT").unlink()
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("Missing artifact", str(caught.exception))

    def test_mutation_during_archiving_is_rejected(self) -> None:
        real_write = selection_client._write_member

        def tamper_after_write(output, path, name, *, limit):
            result = real_write(output, path, name, limit=limit)
            original = Path(path).read_bytes()
            Path(path).write_bytes(bytes(len(original)))
            return result

        with (
            patch.object(selection_client, "_write_member", side_effect=tamper_after_write),
            self.assertRaises(PipelineError) as caught,
        ):
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("changed while archiving", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_symlink_member_is_rejected(self) -> None:
        os.symlink(self.source / "零件.SLDPRT", self.source / "link.SLDPRT")
        index = {**self.upload, "link.SLDPRT": self._sha("零件.SLDPRT")}
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, index)
        self.assertIn("Symlink", str(caught.exception))

    def test_archive_inside_the_source_is_rejected(self) -> None:
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.source / "staging.zip", self.upload)
        self.assertIn("outside the source tree", str(caught.exception))

    def test_nonportable_or_aliased_names_are_rejected(self) -> None:
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, {"../evil": "0" * 64})
        self.assertEqual(getattr(caught.exception, "code", ""), "selection_path_invalid")
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(
                self.source,
                self.archive,
                {**self.upload, "DECOY.SLDPRT": "0" * 64, "decoy.SLDPRT": "0" * 64},
            )
        self.assertEqual(getattr(caught.exception, "code", ""), "selection_path_collision")

    def test_oversized_preflight_reads_no_content(self) -> None:
        with (
            patch.object(selection_client, "MAX_BATCH_BYTES", 4),
            patch.object(selection_client, "_disk_digest", side_effect=AssertionError("content read")),
            self.assertRaises(PipelineError) as caught,
        ):
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("byte cap", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_growth_beyond_the_recorded_size_is_bounded(self) -> None:
        path = self.source / "零件.SLDPRT"
        with self.assertRaises(PipelineError) as caught:
            selection_client._disk_digest(path, limit=4)
        self.assertIn("recorded size", str(caught.exception))
        probe = self.root / "probe.zip"
        with zipfile.ZipFile(probe, "x", compression=zipfile.ZIP_STORED) as output:
            with self.assertRaises(PipelineError) as caught:
                selection_client._write_member(output, path, "零件.SLDPRT", limit=4)
            self.assertIn("recorded size", str(caught.exception))

    def test_source_under_linked_parent_is_rejected(self) -> None:
        real = self.root / "real"
        (real / "intake").mkdir(parents=True)
        link = self.root / "link"
        os.symlink(real, link)
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(link / "intake", self.archive, self.upload)
        self.assertIn("links or junctions", str(caught.exception))

    def test_existing_archive_is_never_clobbered(self) -> None:
        self.archive.write_bytes(b"pre-existing bytes")
        with self.assertRaises(PipelineError) as caught:
            prepare_staging_archive(self.source, self.archive, self.upload)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(self.archive.read_bytes(), b"pre-existing bytes")


if __name__ == "__main__":
    unittest.main()
