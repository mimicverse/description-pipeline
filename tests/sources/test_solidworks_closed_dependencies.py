"""Closed-document dependency query: no OpenDoc, no search, exact parsing."""

from __future__ import annotations

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    closed_document_dependencies,
)

BS = chr(92)
MAIN = BS.join(("C:", "packages", "262ed241", "3.0 总装1008.SLDASM"))


class FakeApp:
    """Records the exact call and never exposes OpenDoc6."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    def GetDocumentDependencies2(self, path, traverse, search, read_only):  # noqa: N802
        self.calls.append((path, traverse, search, read_only))
        return self.payload


class ClosedDocumentDependencyTests(unittest.TestCase):
    def test_default_call_is_no_traverse_no_search_no_read_only_info(self) -> None:
        app = FakeApp(["part.SLDPRT", BS.join(("C:", "pkg", "part.SLDPRT"))])
        entries = closed_document_dependencies(app, MAIN)
        self.assertEqual(app.calls, [(MAIN, False, False, False)])
        self.assertEqual(
            entries,
            [{"name": "part.SLDPRT", "path": BS.join(("C:", "pkg", "part.SLDPRT"))}],
        )

    def test_bare_names_join_against_the_document_directory(self) -> None:
        app = FakeApp(["000_femur.SLDPRT", "000_femur.SLDPRT"])
        entries = closed_document_dependencies(app, MAIN)
        self.assertEqual(
            entries[0]["path"],
            BS.join(("C:", "packages", "262ed241", "000_femur.SLDPRT")),
        )

    def test_absolute_paths_pass_through(self) -> None:
        external = BS.join(("D:", "M2.2", "change11", "HD-1910-C001-20260902.stp"))
        app = FakeApp(["HD-1910-C001-20260902.stp", external])
        entries = closed_document_dependencies(app, MAIN)
        self.assertEqual(entries[0]["path"], external)

    def test_read_only_info_parses_three_string_groups(self) -> None:
        app = FakeApp(
            [
                "part.SLDPRT",
                BS.join(("C:", "pkg", "part.SLDPRT")),
                True,
            ]
        )
        entries = closed_document_dependencies(app, MAIN, add_read_only_info=True)
        self.assertEqual(app.calls, [(MAIN, False, False, True)])
        self.assertEqual(entries[0]["read_only"], True)

    def test_empty_result_is_an_empty_list(self) -> None:
        app = FakeApp(None)
        self.assertEqual(closed_document_dependencies(app, MAIN), [])

    def test_malformed_array_is_refused(self) -> None:
        app = FakeApp(["only-a-name"])
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN)
        self.assertEqual(caught.exception.code, "dependency_list_invalid")

    def test_non_string_name_is_refused(self) -> None:
        app = FakeApp([123, BS.join(("C:", "pkg", "part.SLDPRT"))])
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN)
        self.assertEqual(caught.exception.code, "dependency_list_invalid")

    def test_document_cap_is_refused_explicitly(self) -> None:
        payload = []
        for index in range(3):
            payload.extend([f"p{index}.SLDPRT", BS.join(("C:", "pkg", f"p{index}.SLDPRT"))])
        app = FakeApp(payload)
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN, max_documents=2)
        self.assertEqual(caught.exception.code, "dependency_list_too_large")

    def test_no_open_document_call_is_attempted(self) -> None:
        app = FakeApp([])
        closed_document_dependencies(app, MAIN)
        # FakeApp exposes only GetDocumentDependencies2; any OpenDoc6 attribute
        # access would have raised AttributeError instead of returning.
        self.assertFalse(hasattr(app, "OpenDoc6"))


if __name__ == "__main__":
    unittest.main()
