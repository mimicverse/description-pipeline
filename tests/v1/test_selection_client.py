"""Selection-batch ZIP helper: exact authorization, no folder walks.

The helper is the Linux-side building block for the selection rounds: it may
only archive the names a native request asked for, authorized by the upload
index, with identical hashes before, during and after archiving.
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
from description_pipeline.orchestration.selection_client import prepare_selection_batch


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
        self.archive = self.root / "batch.zip"

    def _request(self, *names: str) -> dict[str, str]:
        return {name: self.upload[name] for name in names}

    def _sha(self, name: str) -> str:
        return hashlib.sha256((self.source / name).read_bytes()).hexdigest()

    def test_batch_archives_only_the_requested_names(self) -> None:
        requested = self._request("总装.SLDASM", "deps/子零件.SLDPRT")
        result = prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertEqual(result["files"], requested)
        self.assertEqual(result["bytes"], sum((self.source / name).stat().st_size for name in requested))
        with zipfile.ZipFile(self.archive) as sealed:
            self.assertEqual(sorted(sealed.namelist()), sorted(requested))
            for name, digest in requested.items():
                self.assertEqual(hashlib.sha256(sealed.read(name)).hexdigest(), digest)
            # Unrelated CAD and non-CAD decoys never enter the batch.
            self.assertNotIn("decoy.SLDPRT", sealed.namelist())
            self.assertNotIn("notes.txt", sealed.namelist())
        self.assertTrue((self.source / "decoy.SLDPRT").is_file())

    def test_request_outside_the_upload_index_is_rejected(self) -> None:
        requested = self._request("decoy.SLDPRT")
        without_decoy = {name: digest for name, digest in self.upload.items() if name != "decoy.SLDPRT"}
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=without_decoy)
        self.assertIn("outside the upload index", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_requested_hash_must_agree_with_the_upload_index(self) -> None:
        requested = {"零件.SLDPRT": "0" * 64}
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertIn("disagree with the upload index", str(caught.exception))

    def test_empty_request_is_rejected(self) -> None:
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, {}, expected_upload=self.upload)
        self.assertIn("nonempty", str(caught.exception))

    def test_oversized_and_overcount_batches_are_rejected(self) -> None:
        requested = self._request("总装.SLDASM", "零件.SLDPRT")
        with patch.object(selection_client, "MAX_BATCH_FILES", 1), self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertIn("file cap", str(caught.exception))
        with patch.object(selection_client, "MAX_BATCH_BYTES", 4), self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertIn("byte cap", str(caught.exception))

    def test_changed_member_is_rejected_before_archiving(self) -> None:
        requested = self._request("零件.SLDPRT")
        (self.source / "零件.SLDPRT").write_bytes(b"changed after upload")
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertIn("changed before archiving", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_missing_member_is_rejected(self) -> None:
        requested = self._request("零件.SLDPRT")
        (self.source / "零件.SLDPRT").unlink()
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertIn("Missing artifact", str(caught.exception))

    def test_mutation_during_archiving_is_rejected(self) -> None:
        requested = self._request("零件.SLDPRT")
        real_write = selection_client._write_member

        def tamper_after_write(output, path, name):
            result = real_write(output, path, name)
            Path(path).write_bytes(b"mutated during archiving")
            return result

        with (
            patch.object(selection_client, "_write_member", side_effect=tamper_after_write),
            self.assertRaises(PipelineError) as caught,
        ):
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=self.upload)
        self.assertIn("changed while archiving", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_symlink_member_is_rejected(self) -> None:
        os.symlink(self.source / "零件.SLDPRT", self.source / "link.SLDPRT")
        expected = {**self.upload, "link.SLDPRT": self._sha("零件.SLDPRT")}
        requested = {"link.SLDPRT": expected["link.SLDPRT"]}
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.archive, requested, expected_upload=expected)
        self.assertIn("Symlink", str(caught.exception))

    def test_archive_inside_the_source_is_rejected(self) -> None:
        requested = self._request("零件.SLDPRT")
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(self.source, self.source / "batch.zip", requested, expected_upload=self.upload)
        self.assertIn("outside the source tree", str(caught.exception))

    def test_nonportable_or_aliased_names_are_rejected(self) -> None:
        requested = self._request("零件.SLDPRT")
        with self.assertRaises(PipelineError):
            prepare_selection_batch(self.source, self.archive, requested, expected_upload={"../evil": "0" * 64})
        with self.assertRaises(PipelineError) as caught:
            prepare_selection_batch(
                self.source,
                self.archive,
                requested,
                expected_upload={**self.upload, "DECOY.SLDPRT": "0" * 64, "decoy.SLDPRT": "0" * 64},
            )
        self.assertIn("casefold duplicate", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
