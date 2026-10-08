"""Known occurrence interfaces and zero-argument methods keep their COM contract."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from description_pipeline.sources.solidworks.native import _component, _method


class ComponentBindingTests(unittest.TestCase):
    def setUp(self):
        self.generic = Mock()
        self.vendor = object()
        self.generic.QueryInterface.return_value = self.vendor
        self.raw = types.SimpleNamespace(_oleobj_=self.generic)
        self.component = types.SimpleNamespace(
            _FlagAsMethod=Mock(), GetPathName=Mock(spec=lambda: None, return_value="C:/copy/arm.SLDPRT")
        )
        pc = types.ModuleType("pythoncom")
        pc.IID_IDispatch = "IDispatch"
        wt = types.ModuleType("pywintypes")
        wt.IID = lambda value: value
        self.dynamic = types.ModuleType("win32com.client.dynamic")
        self.dynamic.DumbDispatch = Mock(return_value=self.component)
        client = types.ModuleType("win32com.client")
        client.dynamic = self.dynamic
        wc = types.ModuleType("win32com")
        wc.client = client
        self.modules = {
            "pythoncom": pc, "pywintypes": wt, "win32com": wc,
            "win32com.client": client, "win32com.client.dynamic": self.dynamic,
        }

    def test_occurrence_reads_use_the_published_vendor_interface(self):
        with patch.dict(sys.modules, self.modules):
            comp = _component(self.raw)
            self.assertEqual(_method(comp, "GetPathName"), "C:/copy/arm.SLDPRT")
        self.generic.QueryInterface.assert_called_once_with(
            "{655D6F2A-5441-45D1-8CBA-D35FB26988E4}", "IDispatch"
        )
        self.dynamic.DumbDispatch.assert_called_once_with(self.vendor)
        self.component._FlagAsMethod.assert_called_once_with("GetPathName")
        self.component.GetPathName.assert_called_once_with()

    def test_unavailable_vendor_interface_has_no_generic_fallback_or_retry(self):
        error = RuntimeError("Native occurrence interface unavailable")
        self.generic.QueryInterface.side_effect = error
        with patch.dict(sys.modules, self.modules), self.assertRaises(RuntimeError) as caught:
            _component(self.raw)
        self.assertIs(caught.exception, error)
        self.generic.QueryInterface.assert_called_once()
        self.dynamic.DumbDispatch.assert_not_called()

    def test_method_name_resolution_error_retains_its_original_cause(self):
        error = RuntimeError("RPC failure while resolving the native method")
        self.component._FlagAsMethod.side_effect = error
        with patch.dict(sys.modules, self.modules), self.assertRaises(RuntimeError) as caught:
            _method(_component(self.raw), "GetPathName")
        self.assertIs(caught.exception, error)
        self.component.GetPathName.assert_not_called()

    def test_non_com_test_values_and_absent_optional_reference_keep_identity(self):
        value = object()
        self.assertIs(_component(value), value)
        self.assertIsNone(_component(None))


if __name__ == "__main__":
    unittest.main()
