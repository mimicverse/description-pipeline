"""Property and method dispatch remain explicit and locally scoped."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.sources.solidworks import native
from description_pipeline.sources.solidworks.native import (
    CadError,
    _indexed_property,
    _method,
    _property,
)


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
        first = _method(app, "GetOpenDocumentByName", "first.SLDPRT")
        app.value = object()
        second = _method(app, "GetOpenDocumentByName", "second.SLDPRT")
        self.assertIsNot(first, second)
        self.assertIs(second, app.value)
        self.assertIs(_method(app, "GetOpenDocumentByName", "third.SLDPRT"), app.value)
        self.assertEqual(app.hints, ["GetOpenDocumentByName"])
        self.assertEqual(
            app.calls,
            [
                ("GetOpenDocumentByName", ("first.SLDPRT",)),
                ("GetOpenDocumentByName", ("second.SLDPRT",)),
                ("GetOpenDocumentByName", ("third.SLDPRT",)),
            ],
        )

    def test_parameterless_method_still_invokes_each_time(self):
        doc = Dispatch()
        first = _method(doc, "GetPathName")
        doc.value = object()
        self.assertIsNot(first, _method(doc, "GetPathName"))
        self.assertEqual(doc.hints, ["GetPathName"])
        self.assertEqual(doc.calls, [("GetPathName", ()), ("GetPathName", ())])

    def test_method_metadata_does_not_cross_owned_dispatches(self):
        source, copy = Dispatch(), Dispatch()
        _method(source, "GetOpenDocumentByName", "source.SLDPRT")
        _method(copy, "GetOpenDocumentByName", "copy.SLDPRT")
        self.assertEqual(source.hints, ["GetOpenDocumentByName"])
        self.assertEqual(copy.hints, ["GetOpenDocumentByName"])
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(len(copy.calls), 1)

    def test_metadata_failure_propagates_before_any_method_invocation(self):
        app = Dispatch()
        error = MetadataError("RPC failed during initial GetIDsOfNames")
        app.metadata_error = error
        with self.assertRaises(MetadataError) as caught:
            _method(app, "GetOpenDocumentByName", "part.SLDPRT")
        self.assertIs(caught.exception, error)
        self.assertEqual(app.hints, [])
        self.assertEqual(app.calls, [])

    def test_native_method_failure_propagates_without_retry(self):
        app = Dispatch()
        error = RuntimeError("native document lookup failed")
        app.call_error = error
        with self.assertRaises(RuntimeError) as caught:
            _method(app, "GetOpenDocumentByName", "part.SLDPRT")
        self.assertIs(caught.exception, error)
        self.assertEqual(app.hints, ["GetOpenDocumentByName"])
        self.assertEqual(app.calls, [("GetOpenDocumentByName", ("part.SLDPRT",))])

    def test_property_read_never_flags_or_invokes_a_method(self):
        class PropertyDispatch:
            def __init__(self):
                self.value = object()

            def __getattr__(self, name):
                if name == "_FlagAsMethod":
                    raise AssertionError("properties must not be method-flagged")
                if name == "Name":
                    return self.value
                raise AttributeError(name)

        dispatch = PropertyDispatch()
        first = _property(dispatch, "Name")
        dispatch.value = object()
        second = _property(dispatch, "Name")
        self.assertIsNot(first, second)
        self.assertIs(second, dispatch.value)

    def test_callable_com_property_binds_without_method_dispatch(self) -> None:
        class CallableComProperty:
            _oleobj_ = object()

            def __call__(self):
                raise AssertionError("a property value must not be invoked")

        class PropertyDispatch:
            def __getattr__(self, name):
                if name == "_FlagAsMethod":
                    raise AssertionError("properties must not be method-flagged")
                if name == "Extension":
                    return CallableComProperty()
                raise AttributeError(name)

        sentinel = object()
        with patch.object(native, "_interface", return_value=sentinel):
            self.assertIs(_property(PropertyDispatch(), "Extension"), sentinel)

    def test_property_read_rejects_a_zero_argument_method(self):
        class MethodOnly:
            def GetPathName(self):
                return "C:\\part.SLDPRT"

        dispatch = MethodOnly()
        with self.assertRaises(CadError) as caught:
            _property(dispatch, "GetPathName")
        self.assertEqual(caught.exception.code, "cad_member_not_property")

    def test_indexed_property_is_a_raw_com_property_get(self) -> None:
        result = type("ComResult", (), {"_oleobj_": object()})()
        raw = object()
        invoke_calls = []
        ole = SimpleNamespace(
            GetIDsOfNames=lambda name: 83 if name == "CustomPropertyManager" else -1,
            Invoke=lambda *args: invoke_calls.append(args) or raw,
        )

        class IndexedDispatch:
            _oleobj_ = ole

            def __getattr__(self, name):
                if name == "_FlagAsMethod":
                    raise AssertionError(
                        "indexed properties must not be method-flagged"
                    )
                if name.startswith("get_"):
                    raise AssertionError(
                        "indexed properties must not use generated aliases"
                    )
                raise AttributeError(name)

        sentinel = object()
        with (
            patch.dict(
                "sys.modules",
                {
                    "pythoncom": SimpleNamespace(
                        DISPATCH_PROPERTYGET=2,
                    )
                },
            ),
            patch.object(
                native,
                "_win32",
                return_value=SimpleNamespace(Dispatch=lambda value: result),
            ),
            patch.object(native, "_dynamic") as dynamic,
            patch.object(native, "_interface", return_value=sentinel) as bind,
        ):
            self.assertIs(
                _indexed_property(
                    IndexedDispatch(),
                    "CustomPropertyManager",
                    "Default",
                ),
                sentinel,
            )
        dynamic.assert_not_called()
        self.assertEqual(
            invoke_calls,
            [(83, 0, 2, 1, "Default")],
        )
        bind.assert_called_once_with(
            result,
            native.ICUSTOMPROPERTYMANAGER_IID,
        )
