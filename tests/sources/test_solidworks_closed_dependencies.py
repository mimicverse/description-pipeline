"""Closed-document dependency query: one documented call, exact stored fields."""

from __future__ import annotations

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    closed_document_dependencies,
)

BS = chr(92)
MAIN = BS.join(("C:", "packages", "fixture", "总装.SLDASM"))


class FakeApp:
    """Records the exact call and never exposes OpenDoc6."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple] = []

    def GetDocumentDependencies2(self, path, traverse, search, read_only):  # noqa: N802
        self.calls.append((path, traverse, search, read_only))
        return self.payload


class ClosedDocumentDependencyTests(unittest.TestCase):
    def test_call_is_exactly_no_traverse_no_search_no_read_only(self) -> None:
        location = BS.join(("C:", "pkg", "part.SLDPRT"))
        app = FakeApp(["part.SLDPRT", location])
        entries = closed_document_dependencies(app, MAIN)
        self.assertEqual(app.calls, [(MAIN, False, False, False)])
        self.assertEqual(
            entries,
            [{"name": "part.SLDPRT", "location": location, "path": location}],
        )

    def test_stored_name_and_location_are_preserved_verbatim(self) -> None:
        location = BS.join(("D:", "旧工程 - 副本", "components", "external-shape.stp"))
        app = FakeApp(["external-shape.stp", location])
        entries = closed_document_dependencies(app, MAIN)
        self.assertEqual(entries[0]["name"], "external-shape.stp")
        self.assertEqual(entries[0]["location"], location)
        self.assertEqual(entries[0]["path"], location)

    def test_com_tuple_result_preserves_each_dependency(self) -> None:
        location = BS.join(("C:", "pkg", "part.SLDPRT"))
        app = FakeApp(("part.SLDPRT", location))
        self.assertEqual(
            closed_document_dependencies(app, MAIN),
            [{"name": "part.SLDPRT", "location": location, "path": location}],
        )

    def test_the_query_options_are_not_parameters(self) -> None:
        app = FakeApp([])
        for keyword in ("traverse", "search", "add_read_only_info"):
            with self.assertRaises(TypeError):
                closed_document_dependencies(app, MAIN, **{keyword: True})
        self.assertEqual(app.calls, [])

    def test_bare_names_join_against_the_document_directory(self) -> None:
        app = FakeApp(["000_femur.SLDPRT", "000_femur.SLDPRT"])
        entries = closed_document_dependencies(app, MAIN)
        self.assertEqual(
            entries[0]["path"],
            BS.join(("C:", "packages", "fixture", "000_femur.SLDPRT")),
        )
        self.assertEqual(entries[0]["name"], "000_femur.SLDPRT")
        self.assertEqual(entries[0]["location"], "000_femur.SLDPRT")

    def test_empty_result_is_an_empty_list(self) -> None:
        for payload in (None, [], ()):
            app = FakeApp(payload)
            self.assertEqual(closed_document_dependencies(app, MAIN), [])
            self.assertEqual(app.calls, [(MAIN, False, False, False)])

    def test_missing_location_is_refused_instead_of_falling_back_to_the_name(self) -> None:
        for payload in (None, "", "   "):
            app = FakeApp(["part.SLDPRT", payload])
            with self.assertRaises(CadError) as caught:
                closed_document_dependencies(app, MAIN)
            self.assertEqual(caught.exception.code, "dependency_location_missing")

    def test_non_string_location_is_refused(self) -> None:
        app = FakeApp(["part.SLDPRT", 123])
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN)
        self.assertEqual(caught.exception.code, "dependency_location_missing")

    def test_non_array_result_is_refused(self) -> None:
        for payload in ("part.SLDPRT", b"part.SLDPRT"):
            app = FakeApp(payload)
            with self.assertRaises(CadError) as caught:
                closed_document_dependencies(app, MAIN)
            self.assertEqual(caught.exception.code, "dependency_list_invalid")

    def test_odd_entry_count_is_refused(self) -> None:
        app = FakeApp(["only-a-name"])
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN)
        self.assertEqual(caught.exception.code, "dependency_list_invalid")

    def test_non_string_name_is_refused(self) -> None:
        app = FakeApp([123, BS.join(("C:", "pkg", "part.SLDPRT"))])
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN)
        self.assertEqual(caught.exception.code, "dependency_list_invalid")

    def test_blank_name_is_refused(self) -> None:
        app = FakeApp(["", BS.join(("C:", "pkg", "part.SLDPRT"))])
        with self.assertRaises(CadError) as caught:
            closed_document_dependencies(app, MAIN)
        self.assertEqual(caught.exception.code, "dependency_list_invalid")

    def test_invalid_document_cap_is_refused_before_any_call(self) -> None:
        for bad in (True, False, 0, -1, 2.5, "16"):
            app = FakeApp([])
            with self.assertRaises(CadError) as caught:
                closed_document_dependencies(app, MAIN, max_documents=bad)
            self.assertEqual(caught.exception.code, "dependency_limit_invalid")
            self.assertEqual(app.calls, [])

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
