"""Independent verification of capture skips for dangling stored CAD references."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.verification.solidworks_urdf import _stale_stored_reference_skips

#: The real candidate4 stored reference: an author-machine path outside every admitted root.
LOCATION = r"C:\Users\operator\Desktop\M3.0\CopyDesignTemp\lowerbody.SLDASM"
STAGE_ROOT = r"C:\stage"
STAGED_LOCATION = STAGE_ROOT + r"\lowerbody.SLDASM"
UNRELATED = r"C:\description-v1-review\native-candidate\state\prepared\CopyDesignTemp\lowerbody.SLDASM"


class StaleStoredReferenceSkipTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="stale-skip-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "evidence").mkdir(parents=True)

    def _write_payload(self, *, files=None, documents=None) -> None:
        write_json(
            self.root / "evidence/discovery.json",
            {
                "raw": {
                    "source_selection": {
                        "files": files if files is not None else {"shank.SLDPRT": "a" * 64},
                        "closure": {
                            "stage_root": STAGE_ROOT,
                            "documents": documents
                            if documents is not None
                            else {"shank.SLDPRT": self._document(LOCATION)},
                        },
                    }
                }
            },
        )

    @staticmethod
    def _document(location, edges=None) -> dict:
        return {
            "stored_references": [{"name": "lowerbody", "location": location}],
            "resolved_edges": edges or [],
        }

    @staticmethod
    def _entry(location=LOCATION, resolved_path=None, document="shank.SLDPRT", exists=False) -> dict:
        return {
            "document": document,
            "location": location,
            "resolved_path": resolved_path or location,
            "exists": exists,
        }

    @staticmethod
    def _closure(skips, *, original_files=None, mapping=None) -> dict:
        return {
            "stale_stored_references": skips,
            "original_files": original_files or {},
            "mapping": mapping or [],
        }

    def test_validated_skip_normalization_and_defaults(self) -> None:
        self._write_payload()
        cases = (
            self._entry(),
            self._entry(resolved_path=LOCATION.replace("CopyDesignTemp", "copyDesigntemp")),
            self._entry(resolved_path="C:/Users/operator/Desktop/M3.0/CopyDesignTemp/lowerbody.SLDASM"),
        )
        for entry in cases:
            with self.subTest(entry=entry["resolved_path"]):
                self.assertEqual(_stale_stored_reference_skips(self.root, self._closure([entry])), 1)
        self.assertEqual(_stale_stored_reference_skips(self.root, {}), 0)
        self.assertEqual(_stale_stored_reference_skips(self.root, {"stale_stored_references": []}), 0)

    def test_non_list_records_are_refused(self) -> None:
        for label, record in (
            ("empty_mapping", {}),
            ("empty_string", ""),
            ("zero", 0),
            ("entry_mapping", {"document": "shank.SLDPRT"}),
        ):
            with self.subTest(case=label):
                self._write_payload()
                with self.assertRaises(PipelineError):
                    _stale_stored_reference_skips(self.root, {"stale_stored_references": record})

    def test_replay_entries_require_a_validated_covering_path(self) -> None:
        self._write_payload()
        replay = {"document": None, "location": None, "resolved_path": LOCATION, "exists": False}
        self.assertEqual(_stale_stored_reference_skips(self.root, self._closure([self._entry(), replay])), 2)
        cases = {
            "uncovered_replay": [replay],
            "replay_other_path": [self._entry(), {**replay, "resolved_path": LOCATION + ".other"}],
        }
        for label, skips in cases.items():
            with self.subTest(case=label), self.assertRaises(PipelineError):
                _stale_stored_reference_skips(self.root, self._closure(skips))

    def test_fake_malformed_or_unbound_skip_is_refused(self) -> None:
        cases = {
            "fake_location": [self._entry(location=LOCATION + ".other")],
            "changed_case_location": [self._entry(location=LOCATION.replace("CopyDesignTemp", "copyDesigntemp"))],
            "unrelated_resolved_path": [self._entry(resolved_path=UNRELATED)],
            "unknown_document": [self._entry(document="ghost.SLDPRT")],
            "missing_resolved_path": [{"document": "shank.SLDPRT", "location": LOCATION}],
            "relative_resolved_path": [self._entry(resolved_path="lowerbody.SLDASM")],
            "exists_not_boolean": [{**self._entry(), "exists": "false"}],
        }
        for label, skips in cases.items():
            with self.subTest(case=label):
                self._write_payload()
                with self.assertRaises(PipelineError):
                    _stale_stored_reference_skips(self.root, self._closure(skips))

    def test_required_or_active_skip_is_refused(self) -> None:
        active_edge = {
            "name": "lb-1",
            "kind": "native_document",
            "resolved": "lowerbody.SLDASM",
            "resolved_path": LOCATION,
        }
        other_edge = {"name": "x-1", "kind": "native_document", "resolved": "x.SLDPRT", "resolved_path": LOCATION}
        cases = (
            ("required_original_file", None, {"original_files": {LOCATION: "b" * 64}}, None),
            (
                "required_mapping_source",
                None,
                {"mapping": [{"source": LOCATION, "copy": "s/x", "instance": "p"}]},
                None,
            ),
            ("active_edge_path", [active_edge], {}, None),
            (
                "cross_document_active_path",
                [],
                {},
                {"other.SLDPRT": {"stored_references": [], "resolved_edges": [other_edge]}},
            ),
        )
        for label, edges, dependency, extra in cases:
            with self.subTest(case=label):
                extra = extra or {}
                extra.setdefault("shank.SLDPRT", self._document(LOCATION, edges))
                self._write_payload(documents=extra)
                closure = self._closure(
                    [self._entry()],
                    original_files=dependency.get("original_files"),
                    mapping=dependency.get("mapping"),
                )
                with self.assertRaises(PipelineError):
                    _stale_stored_reference_skips(self.root, closure)

    def test_stage_containment_selected_and_resolved_target_are_refused(self) -> None:
        edge = {
            "name": "lb-1",
            "kind": "native_document",
            "resolved": "lowerbody.SLDASM",
            "resolved_path": r"C:\session\lowerbody.SLDASM",
        }
        cases = {
            "selected_containment": {
                "files": {"shank.SLDPRT": "a" * 64, "lowerbody.SLDASM": "b" * 64},
                "edges": [],
            },
            "resolved_target_containment": {"files": None, "edges": [edge]},
        }
        for label, spec in cases.items():
            with self.subTest(case=label):
                self._write_payload(
                    files=spec["files"],
                    documents={"shank.SLDPRT": self._document(STAGED_LOCATION, spec["edges"])},
                )
                with self.assertRaises(PipelineError):
                    _stale_stored_reference_skips(self.root, self._closure([self._entry(location=STAGED_LOCATION)]))

    def test_relative_location_binds_through_the_scanning_document_source(self) -> None:
        document_source = r"C:\prepared\shank.SLDPRT"
        self._write_payload(documents={"shank.SLDPRT": self._document("lowerbody.SLDASM")})
        entry = self._entry(location="lowerbody.SLDASM", resolved_path=r"C:\prepared\lowerbody.SLDASM")
        closure = self._closure([entry], original_files={document_source: "a" * 64})
        self.assertEqual(_stale_stored_reference_skips(self.root, closure), 1)
        for label, files, resolved in (
            ("wrong_join", {document_source: "a" * 64}, r"C:\elsewhere\lowerbody.SLDASM"),
            ("no_candidate", {}, r"C:\prepared\lowerbody.SLDASM"),
        ):
            with self.subTest(case=label):
                closure = self._closure(
                    [self._entry(location="lowerbody.SLDASM", resolved_path=resolved)],
                    original_files=files,
                )
                with self.assertRaises(PipelineError):
                    _stale_stored_reference_skips(self.root, closure)


if __name__ == "__main__":
    unittest.main()
