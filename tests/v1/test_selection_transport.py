"""Transport proof: exclusive bounded extraction, staging identity and closure sealing."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import inventory
from description_pipeline.orchestration import selection_transport
from description_pipeline.orchestration.selection_transport import StagedSelection
from description_pipeline.sources.solidworks.selection import SelectionError

from tests.sources.selection_support import closure_evidence

MAIN = "root.SLDASM"
DATA = {
    "root.SLDASM": b"synthetic assembly",
    "parts/link.SLDPRT": b"synthetic referenced part",
    "decoy.SLDPRT": b"unrelated native document",
    "notes.txt": b"unrelated notes",
}
EDGES = {MAIN: ["parts/link.SLDPRT"], "parts/link.SLDPRT": [], "decoy.SLDPRT": []}


class SelectionTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upload = {name: hashlib.sha256(data).hexdigest() for name, data in DATA.items()}
        self.reader = "a" * 64
        self.stage_root = str(self.root / "stage")

    def _session(self):
        return StagedSelection(self.root / "stage", self.upload, MAIN, reader_sha256=self.reader)

    def _archive(self, path: Path, items: dict[str, bytes]) -> Path:
        with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as sink:
            for name, data in items.items():
                sink.writestr(name, data)
        return path

    def _evidence(self) -> dict:
        return closure_evidence(
            self.upload,
            MAIN,
            edges=EDGES,
            reader_sha256=self.reader,
            stage_root=self.stage_root,
        )

    def test_exact_full_index_installs_once_and_seals(self) -> None:
        session = self._session()
        self.assertTrue(session.accept_batch(self._archive(self.root / "batch.zip", DATA)))
        self.assertFalse(session.accept_batch(self._archive(self.root / "retry.zip", DATA)))
        manifest = session.seal_closure(self._evidence())
        self.assertEqual(set(manifest["files"]), {MAIN, "parts/link.SLDPRT"})
        self.assertEqual(set(manifest["excluded_files"]), {"decoy.SLDPRT", "notes.txt"})
        self.assertEqual(inventory(session.stage), manifest["files"])
        self.assertEqual(session.snapshot()["staged_files"], self.upload)

    def test_extra_missing_duplicate_and_noncanonical_members_are_refused(self) -> None:
        cases = {
            "extra": {**DATA, "extra.SLDPRT": b"extra"},
            "missing": {name: data for name, data in DATA.items() if name != "decoy.SLDPRT"},
            "traversal": {**DATA, "../evil.SLDPRT": b"evil"},
        }
        for label, items in cases.items():
            with self.subTest(case=label):
                session = StagedSelection(self.root / f"stage-{label}", self.upload, MAIN, reader_sha256=self.reader)
                with self.assertRaises(SelectionError):
                    session.accept_batch(self._archive(self.root / f"{label}.zip", items))
                self.assertEqual(inventory(session.stage), {})
        duplicate = self.root / "duplicate.zip"
        with zipfile.ZipFile(duplicate, "x", zipfile.ZIP_DEFLATED) as sink:
            for name, data in DATA.items():
                sink.writestr(name, data)
            sink.writestr("decoy.SLDPRT", b"duplicate member")
        session = StagedSelection(self.root / "stage-duplicate", self.upload, MAIN, reader_sha256=self.reader)
        with self.assertRaises(SelectionError):
            session.accept_batch(duplicate)
        self.assertEqual(inventory(session.stage), {})

    def test_size_bound_is_enforced_before_extraction(self) -> None:
        session = self._session()
        with patch.object(selection_transport, "MAX_HANDOFF_BYTES", 32), self.assertRaises(SelectionError):
            session.accept_batch(self._archive(self.root / "batch.zip", DATA))
        self.assertEqual(inventory(session.stage), {})

    def test_changed_stage_refuses_the_closure_seal(self) -> None:
        session = self._session()
        self.assertTrue(session.accept_batch(self._archive(self.root / "batch.zip", DATA)))
        (session.stage / "parts/link.SLDPRT").write_bytes(b"tampered after staging")
        with self.assertRaises(SelectionError):
            session.seal_closure(self._evidence())
        self.assertEqual(session.snapshot()["state"], "reading")


if __name__ == "__main__":
    unittest.main()
