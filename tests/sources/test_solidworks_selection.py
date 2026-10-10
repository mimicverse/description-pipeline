"""Only assembly-derived file requests can become an admitted source set."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.io import digest
from description_pipeline.sources.solidworks.selection import (
    SelectionError,
    seal_selection,
    selection_plan,
    validate_file_index,
    verify_selection,
)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.main = "总装.SLDASM"
        self.files = {
            self.main: "a" * 64,
            "module/arm.SLDASM": "b" * 64,
            "parts/link.SLDPRT": "c" * 64,
            "sources/shape.step": "d" * 64,
            "unused.SLDASM": "e" * 64,
            "manual.urdf": "f" * 64,
            "notes.md": "1" * 64,
        }
        self.reader = "2" * 64

    def observed(self, name, references):
        return {"sha256": self.files[name], "references": references}

    def graph(self):
        return {
            self.main: self.observed(self.main, ["module/arm.SLDASM"]),
            "module/arm.SLDASM": self.observed("module/arm.SLDASM", ["parts/link.SLDPRT"]),
            "parts/link.SLDPRT": self.observed("parts/link.SLDPRT", ["sources/shape.step"]),
            "sources/shape.step": self.observed("sources/shape.step", []),
        }

    def test_initial_request_is_only_the_selected_assembly(self):
        plan = selection_plan(self.files, self.main, {})
        self.assertEqual(plan["state"], "requesting")
        self.assertEqual(plan["requested_files"], {self.main: self.files[self.main]})

    def test_each_round_requests_only_new_reachable_dependencies(self):
        complete = self.graph()
        observations = {}
        for name in complete:
            plan = selection_plan(self.files, self.main, observations)
            self.assertEqual(plan["requested_files"], {name: self.files[name]})
            observations[name] = complete[name]
        plan = selection_plan(self.files, self.main, observations)
        self.assertEqual(plan["state"], "complete")
        self.assertEqual(plan["requested_files"], {})
        self.assertEqual(set(plan["selected_files"]), set(complete))

    def test_part_references_and_leaf_evidence_are_required(self):
        graph = self.graph()
        graph.pop("sources/shape.step")
        self.assertEqual(
            selection_plan(self.files, self.main, graph)["requested_files"],
            {"sources/shape.step": self.files["sources/shape.step"]},
        )
        with self.assertRaises(SelectionError) as caught:
            seal_selection(self.files, self.main, graph, reader_sha256=self.reader)
        self.assertEqual(caught.exception.code, "selection_incomplete")

    def test_unrelated_observation_is_not_authorized_by_the_upload_index(self):
        graph = self.graph()
        graph["unused.SLDASM"] = self.observed("unused.SLDASM", [])
        with self.assertRaises(SelectionError) as caught:
            selection_plan(self.files, self.main, graph)
        self.assertEqual(caught.exception.code, "selection_unrelated_document")

    def test_external_missing_and_case_guessed_paths_are_refused(self):
        for path in ("D:/old/shape.step", "../sources/shape.step", "absent.SLDPRT", "parts/LINK.SLDPRT"):
            with self.subTest(path=path), self.assertRaises(SelectionError):
                selection_plan(self.files, self.main, {self.main: self.observed(self.main, [path])})

    def test_same_named_files_in_other_directories_are_not_substituted(self):
        files = {**self.files, "other/link.SLDPRT": "3" * 64}
        graph = self.graph()
        graph["module/arm.SLDASM"]["references"] = ["missing/link.SLDPRT"]
        with self.assertRaises(SelectionError) as caught:
            selection_plan(files, self.main, graph)
        self.assertEqual(caught.exception.detail["reference"], "missing/link.SLDPRT")

    def test_changed_document_hash_cannot_be_sealed_as_a_new_source(self):
        graph = self.graph()
        graph[self.main]["sha256"] = "0" * 64
        with self.assertRaises(SelectionError) as caught:
            selection_plan(self.files, self.main, graph)
        self.assertEqual(caught.exception.code, "selection_source_changed")

    def test_graph_cycles_are_finite_file_dependencies_not_a_kinematic_claim(self):
        graph = self.graph()
        graph["parts/link.SLDPRT"]["references"].append(self.main)
        manifest = seal_selection(self.files, self.main, graph, reader_sha256=self.reader)
        self.assertEqual(set(manifest["files"]), set(graph))

    def test_seal_binds_upload_selected_files_and_graph_separately(self):
        graph = self.graph()
        manifest = seal_selection(self.files, self.main, graph, reader_sha256=self.reader)
        self.assertEqual(manifest["upload_sha256"], digest(self.files))
        self.assertEqual(manifest["handoff_sha256"], digest(manifest["files"]))
        self.assertNotEqual(manifest["upload_sha256"], manifest["handoff_sha256"])
        self.assertEqual(set(manifest["excluded_files"]), {"unused.SLDASM", "manual.urdf", "notes.md"})
        self.assertEqual(verify_selection(manifest, self.files, manifest["files"], reader_sha256=self.reader), manifest)
        reordered = dict(reversed(list(self.files.items())))
        self.assertEqual(seal_selection(reordered, self.main, graph, reader_sha256=self.reader), manifest)

    def test_added_missing_or_changed_transferred_bytes_fail(self):
        manifest = seal_selection(self.files, self.main, self.graph(), reader_sha256=self.reader)
        extra = {**manifest["files"], "manual.urdf": self.files["manual.urdf"]}
        missing = {name: checksum for name, checksum in manifest["files"].items() if name != self.main}
        changed = {**manifest["files"], self.main: "0" * 64}
        for files in (extra, missing, changed):
            with self.subTest(files=files), self.assertRaises(SelectionError) as caught:
                verify_selection(manifest, self.files, files, reader_sha256=self.reader)
            self.assertEqual(caught.exception.code, "selection_inventory_changed")

    def test_forged_scope_reader_digest_or_exclusion_is_rejected(self):
        manifest = seal_selection(self.files, self.main, self.graph(), reader_sha256=self.reader)
        for field, value in (
            ("scope", "active_configuration"),
            ("reader_sha256", "0" * 64),
            ("upload_sha256", "0" * 64),
            ("selection_sha256", "0" * 64),
            ("excluded_files", {}),
        ):
            forged = {**manifest, field: value}
            with self.subTest(field=field), self.assertRaises(SelectionError):
                verify_selection(forged, self.files, manifest["files"], reader_sha256=self.reader)

    def test_main_selection_and_observation_shape_are_strict(self):
        for main in ("unused.SLDASM ", "parts/link.SLDPRT", "absent.SLDASM", None):
            with self.subTest(main=main), self.assertRaises(SelectionError):
                selection_plan(self.files, main, {})
        for record in (None, {}, {"sha256": self.files[self.main], "references": None}):
            with self.subTest(record=record), self.assertRaises(SelectionError):
                selection_plan(self.files, self.main, {self.main: record})
        duplicated = copy.deepcopy(self.graph())
        duplicated[self.main]["references"] *= 2
        with self.assertRaises(SelectionError):
            selection_plan(self.files, self.main, duplicated)

    def test_nonportable_and_aliasing_metadata_never_authorizes_files(self):
        for files in (
            {},
            {"x.SLDASM": "not a hash"},
            {"x.SLDASM": "a" * 64, "X.SLDASM": "b" * 64},
            {"X": "a" * 64, "x/child": "b" * 64},
            {"CON.SLDPRT": "a" * 64},
            {"~$x.SLDASM": "a" * 64},
            {"cad//x.SLDASM": "a" * 64},
        ):
            with self.subTest(files=files), self.assertRaises(SelectionError):
                validate_file_index(files)


if __name__ == "__main__":
    unittest.main()
