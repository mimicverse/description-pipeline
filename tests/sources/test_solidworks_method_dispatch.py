"""Method metadata is local to a dispatch; CAD values are read on every call."""

import unittest

from description_pipeline.sources.solidworks.native import _member, _method


class MetadataError(Exception):
    pass


class Dispatch:
    def __init__(self):
        self.hints = []
        self.calls = []
        self.value = object()
        self.metadata_error = None
        self.call_error = None

    def _FlagAsMethod(self, name):
        if self.metadata_error is not None:
            raise self.metadata_error
        if name in self.hints:
            raise MetadataError("RPC failed during redundant GetIDsOfNames")
        self.hints.append(name)

    def __getattr__(self, name):
        if name not in self.hints:
            raise AttributeError(name)

        def invoke(*arguments):
            self.calls.append((name, arguments))
            if self.call_error is not None:
                raise self.call_error
            return self.value

        return invoke


class MethodDispatchTests(unittest.TestCase):
    def test_argument_readers_share_metadata_but_read_fresh_values(self):
        app = Dispatch()
        first = _member(app, "GetOpenDocumentByName", "first.SLDPRT")
        app.value = object()
        second = _member(app, "GetOpenDocumentByName", "second.SLDPRT")
        self.assertIsNot(first, second)
        self.assertIs(second, app.value)
        self.assertIs(_method(app, "GetOpenDocumentByName", "third.SLDPRT"), app.value)
        self.assertEqual(app.hints, ["GetOpenDocumentByName"])
        self.assertEqual(app.calls, [
            ("GetOpenDocumentByName", ("first.SLDPRT",)),
            ("GetOpenDocumentByName", ("second.SLDPRT",)),
            ("GetOpenDocumentByName", ("third.SLDPRT",)),
        ])

    def test_parameterless_method_still_invokes_each_time(self):
        doc = Dispatch()
        first = _method(doc, "GetPathName")
        doc.value = object()
        self.assertIsNot(first, _method(doc, "GetPathName"))
        self.assertEqual(doc.hints, ["GetPathName"])
        self.assertEqual(doc.calls, [("GetPathName", ()), ("GetPathName", ())])

    def test_method_metadata_does_not_cross_owned_dispatches(self):
        source, copy = Dispatch(), Dispatch()
        _member(source, "GetOpenDocumentByName", "source.SLDPRT")
        _member(copy, "GetOpenDocumentByName", "copy.SLDPRT")
        self.assertEqual(source.hints, ["GetOpenDocumentByName"])
        self.assertEqual(copy.hints, ["GetOpenDocumentByName"])
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(len(copy.calls), 1)

    def test_metadata_failure_propagates_before_any_method_invocation(self):
        app = Dispatch()
        error = MetadataError("RPC failed during initial GetIDsOfNames")
        app.metadata_error = error
        with self.assertRaises(MetadataError) as caught:
            _member(app, "GetOpenDocumentByName", "part.SLDPRT")
        self.assertIs(caught.exception, error)
        self.assertEqual(app.hints, [])
        self.assertEqual(app.calls, [])

    def test_native_method_failure_propagates_without_retry(self):
        app = Dispatch()
        error = RuntimeError("native document lookup failed")
        app.call_error = error
        with self.assertRaises(RuntimeError) as caught:
            _member(app, "GetOpenDocumentByName", "part.SLDPRT")
        self.assertIs(caught.exception, error)
        self.assertEqual(app.hints, ["GetOpenDocumentByName"])
        self.assertEqual(app.calls, [("GetOpenDocumentByName", ("part.SLDPRT",))])
