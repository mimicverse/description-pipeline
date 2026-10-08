"""The owned application binds its vendor interface, never the generic ROT view."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from description_pipeline.sources.solidworks.errors import EnvironmentError_
from description_pipeline.sources.solidworks.isolation import application_for_pid

ISLDWORKS = "{83A33D22-27C5-11CE-BFD4-00400513BB57}"


class RpcError(Exception):
    pass


class ApplicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.context = object()
        self.vendor = Mock()
        self.vendor.InvokeTypes.return_value = 327
        self.unknown = Mock()

        def query(iid, use_iid=None):
            if (iid, use_iid) != (ISLDWORKS, "IDispatch"):
                raise RpcError("The generic interface cannot serve RPC")
            return self.vendor

        self.unknown.QueryInterface.side_effect = query
        self.moniker = Mock()
        self.moniker.GetDisplayName.return_value = "SolidWorks_PID_327"
        self.rot = Mock()
        self.rot.GetObject.return_value = self.unknown
        self.rot.__iter__ = Mock(return_value=iter([self.moniker]))
        self.pythoncom = types.ModuleType("pythoncom")
        self.pythoncom.IID_IDispatch = "IDispatch"
        self.pythoncom.DISPATCH_METHOD = 1
        self.pythoncom.VT_I4 = 3
        self.pythoncom.com_error = RpcError
        self.pythoncom.GetRunningObjectTable = Mock(return_value=self.rot)
        self.pythoncom.CreateBindCtx = Mock(return_value=self.context)
        self.pywintypes = types.ModuleType("pywintypes")
        self.pywintypes.IID = Mock(side_effect=lambda value: value)
        self.dynamic = types.ModuleType("win32com.client.dynamic")
        self.dynamic.DumbDispatch = Mock(return_value=object())
        self.client = types.ModuleType("win32com.client")
        self.client.dynamic = self.dynamic
        self.win32com = types.ModuleType("win32com")
        self.win32com.client = self.client
        self.modules = {
            "pythoncom": self.pythoncom,
            "pywintypes": self.pywintypes,
            "win32com": self.win32com,
            "win32com.client": self.client,
            "win32com.client.dynamic": self.dynamic,
        }

    def bind(self):
        with patch.dict(sys.modules, self.modules):
            return application_for_pid(327)

    def test_generic_rpc_failure_does_not_affect_explicit_vendor_binding(self):
        self.assertIs(self.bind(), self.dynamic.DumbDispatch.return_value)
        self.unknown.QueryInterface.assert_called_once_with(ISLDWORKS, "IDispatch")
        self.vendor.InvokeTypes.assert_called_once_with(166, 0, 1, (3, 0), ())
        self.vendor.GetIDsOfNames.assert_not_called()
        self.dynamic.DumbDispatch.assert_called_once_with(self.vendor)
        self.moniker.GetDisplayName.assert_called_once_with(self.context, None)

    def test_foreign_or_unreadable_monikers_never_supply_the_application(self):
        unreadable, foreign = Mock(), Mock()
        unreadable.GetDisplayName.side_effect = RpcError("Unrelated ROT entry")
        foreign.GetDisplayName.return_value = "SolidWorks_PID_999"
        self.rot.__iter__ = Mock(return_value=iter([unreadable, foreign, self.moniker]))
        self.bind()
        self.rot.GetObject.assert_called_once_with(self.moniker)

    def test_absent_owned_moniker_does_not_activate_any_server(self):
        self.moniker.GetDisplayName.return_value = "SolidWorks_PID_999"
        self.assertIsNone(self.bind())
        self.rot.GetObject.assert_not_called()
        self.dynamic.DumbDispatch.assert_not_called()

    def test_wrong_or_invalid_process_identity_stops_before_wrapping(self):
        for value in (999, "327", 327.0, True):
            with self.subTest(value=value):
                self.rot.__iter__ = Mock(return_value=iter([self.moniker]))
                self.vendor.InvokeTypes.return_value = value
                with self.assertRaises(EnvironmentError_) as caught:
                    self.bind()
                self.assertEqual(caught.exception.code, "cad_process_identity_mismatch")
        self.dynamic.DumbDispatch.assert_not_called()

    def test_vendor_query_failure_propagates_without_generic_fallback(self):
        failure = RpcError("Vendor interface unavailable")
        self.unknown.QueryInterface.side_effect = failure
        with self.assertRaises(RpcError) as caught:
            self.bind()
        self.assertIs(caught.exception, failure)
        self.unknown.QueryInterface.assert_called_once_with(ISLDWORKS, "IDispatch")
        self.vendor.InvokeTypes.assert_not_called()
        self.dynamic.DumbDispatch.assert_not_called()

    def test_process_identity_invocation_failure_has_no_retry(self):
        failure = RpcError("Vendor process read failed")
        self.vendor.InvokeTypes.side_effect = failure
        with self.assertRaises(RpcError) as caught:
            self.bind()
        self.assertIs(caught.exception, failure)
        self.vendor.InvokeTypes.assert_called_once_with(166, 0, 1, (3, 0), ())
        self.dynamic.DumbDispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
