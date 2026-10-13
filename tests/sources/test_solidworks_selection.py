"""Pure selection contract: index validation, closure sealing and the verifier check."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.selection import (
    SelectionError,
    reachable_closure,
    seal_closure_selection,
    validate_file_index,
    verify_selection,
)
from description_pipeline.verification.native_discovery import _Failure, _require_source_selection

from tests.sources.selection_support import closure_evidence, sealed_selection

MAIN = "cad/robot.SLDASM"
FILES = {
    "cad/robot.SLDASM": "a" * 64,
    "cad/base.SLDPRT": "b" * 64,
    "cad/arm.SLDPRT": "c" * 64,
    "notes.txt": "d" * 64,
}
EDGES = {MAIN: ["cad/base.SLDPRT", "cad/arm.SLDPRT"], "cad/base.SLDPRT": [], "cad/arm.SLDPRT": []}


class IndexValidationTests(unittest.TestCase):
    def test_index_validation_rejects_nonsha_and_noncanonical_names(self) -> None:
        with self.assertRaises(SelectionError):
            validate_file_index({"a.SLDPRT": "not-a-digest"})
        with self.assertRaises(SelectionError):
            validate_file_index({"../a.SLDPRT": "a" * 64})
        with self.assertRaises(SelectionError):
            validate_file_index({"A.SLDPRT": "a" * 64, "a.SLDPRT": "b" * 64})
        self.assertEqual(validate_file_index(FILES), dict(sorted(FILES.items())))


class ClosureSelectionTests(unittest.TestCase):
    def test_reachability_and_seal(self) -> None:
        graph = {name: {"sha256": FILES[name], "references": refs} for name, refs in EDGES.items()}
        self.assertEqual(reachable_closure(graph, MAIN), {"cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"})
        manifest = sealed_selection(FILES, MAIN, edges=EDGES)
        self.assertEqual(set(manifest["files"]), {"cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"})
        self.assertEqual(set(manifest["excluded_files"]), {"notes.txt"})
        self.assertEqual(
            verify_selection(manifest, FILES, manifest["files"], reader_sha256=manifest["reader_sha256"]), manifest
        )

    def test_main_must_be_an_indexed_assembly(self) -> None:
        evidence = closure_evidence(FILES, MAIN, edges=EDGES)
        with self.assertRaises(SelectionError):
            seal_closure_selection(FILES, "cad/base.SLDPRT", evidence, reader_sha256=evidence["reader_sha256"])
        with self.assertRaises(SelectionError):
            seal_closure_selection(FILES, "missing.SLDASM", evidence, reader_sha256=evidence["reader_sha256"])


class SourceSelectionVerifierTests(unittest.TestCase):
    def _payload(self, manifest: dict, *, include: bool = True, main_choice: str = MAIN) -> dict:
        raw = {
            "files": dict(manifest["files"]),
            "selection": {"main_assembly": main_choice},
        }
        if include:
            raw["source_selection"] = manifest
        return {"raw": raw}

    def test_missing_carrier_is_refused(self) -> None:
        manifest = sealed_selection(FILES, MAIN, edges=EDGES)
        with self.assertRaises(_Failure):
            _require_source_selection(self._payload(manifest, include=False))
        with self.assertRaises(_Failure):
            _require_source_selection({})

    def test_valid_carrier_recomputes_and_binds_the_raw_inventory(self) -> None:
        manifest = sealed_selection(FILES, MAIN, edges=EDGES)
        _require_source_selection(self._payload(manifest))

    def test_tampered_manifest_or_unbound_source_is_refused(self) -> None:
        manifest = sealed_selection(FILES, MAIN, edges=EDGES)
        with self.assertRaises(_Failure):
            _require_source_selection(self._payload({**manifest, "selection_sha256": "00" * 32}))
        raw = self._payload(manifest)
        raw["raw"]["files"] = {"cad/base.SLDPRT": "b" * 64}
        with self.assertRaises(_Failure):
            _require_source_selection(raw)
        with self.assertRaises(_Failure):
            _require_source_selection(self._payload(manifest, main_choice="cad/base.SLDPRT"))


if __name__ == "__main__":
    unittest.main()
