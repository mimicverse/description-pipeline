"""Portable boundary checks; real native evidence is separately hash-bound."""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.sources.solidworks import native as N
from description_pipeline.sources.solidworks.errors import CadError
from tests.sources.test_solidworks_native_reader import _Feature, _com_stubs, _raiser


@contextmanager
def _variants():
    with _com_stubs():
        module = sys.modules["pythoncom"]
        module.VT_ARRAY = 0x2000
        module.VT_UI1 = 17
        module.VT_UNKNOWN = 13
        module.DISPATCH_METHOD = 1
        yield


def _doc(feature=None, extension=None):
    return SimpleNamespace(
        GetPathName=lambda: "/locked/robot.SLDASM",
        FirstFeature=lambda: feature,
        Extension=extension,
    )


def _exporter_doc(xml, version=1.4, name=N.URDF_EXPORTER_ATTRIBUTE_NAME):
    attribute = SimpleNamespace(
        GetName=lambda: name,
        GetParameter=lambda key: SimpleNamespace(
            GetStringValue=lambda: xml,
            GetDoubleValue=lambda: version,
        ),
    )
    feature = _Feature(N.URDF_EXPORTER_ATTRIBUTE_NAME + "1", "Attribute", specific=attribute)
    return _doc(feature, SimpleNamespace())


class ExporterReadTests(unittest.TestCase):
    def test_absent_and_failed_traversal_are_distinct(self):
        result = N._urdf_exporter_attribute(SimpleNamespace(), _doc(), Path("/locked"), "ConfA")
        self.assertEqual(result["state"], "absent")
        doc = _doc()
        doc.FirstFeature = _raiser(RuntimeError("cannot traverse"))
        result = N._urdf_exporter_attribute(SimpleNamespace(), doc, Path("/locked"), "ConfA")
        self.assertEqual(result["state"], "unread")
        self.assertIn("cannot traverse", result["error"])

    def test_read_preserves_exact_xml_hash_configuration_and_version(self):
        calls = []
        definition = SimpleNamespace(
            AddParameter=lambda *args: calls.append(args) or True,
            Register=lambda: True,
        )
        app = SimpleNamespace(DefineAttribute=lambda name: definition)
        xml = "<Robot>\n  <Name>Saved CAD declaration</Name>\n</Robot>"
        with _variants(), patch.object(N, "_relative_document", return_value="robot.SLDASM"):
            result = N._urdf_exporter_attribute(app, _exporter_doc(xml), Path("/locked"), "ConfA")
        self.assertEqual(result["state"], "extracted")
        self.assertEqual(result["raw_xml"], xml)
        self.assertEqual(result["raw_xml_sha256"], hashlib.sha256(xml.encode()).hexdigest())
        self.assertEqual(result["exporter_version"], 1.4)
        self.assertEqual(result["document"], "robot.SLDASM")
        self.assertEqual(result["configuration"], "ConfA")
        self.assertEqual(
            calls, [("data", 1, 0, 0), ("name", 1, 0, 0), ("date", 1, 0, 0), ("exporterVersion", 0, 1.0, 0)]
        )

    def test_unread_xml_and_wrong_attribute_are_never_absent_or_extracted(self):
        for doc in (_exporter_doc(""), _exporter_doc("<broken"), _exporter_doc("<Robot/>", name="other")):
            with self.subTest(doc=doc), _variants():
                result = N._urdf_exporter_attribute(SimpleNamespace(), doc, Path("/locked"), "ConfA")
                self.assertEqual(result["state"], "unread")
                self.assertIn("error", result)

    def test_pid_resolution_uses_exact_unsigned_bytes_and_deduplicates_by_bytes(self):
        payload = b"\x00\x01\x80\xff\x06"
        b64 = base64.b64encode(payload).decode()
        xml = (
            f"<Root><SWComponentPIDs><base64Binary>{b64}</base64Binary></SWComponentPIDs>"
            f"<SWMainComponentPID><base64Binary>{b64}</base64Binary></SWMainComponentPID></Root>"
        )
        received = []
        component = SimpleNamespace(
            Name2="arm-2/part-1", GetPathName=lambda: "/locked/part.SLDPRT", ReferencedConfiguration="SavedConfig"
        )

        def resolve(value, errors):
            received.append(value.value)
            errors.value = 0
            return component

        with _variants():
            rows = N._urdf_exporter_pid_bindings(
                xml, SimpleNamespace(GetObjectByPersistReference3=resolve), Path("/locked")
            )
        self.assertEqual(received, [payload])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid_hex"], payload.hex())
        self.assertEqual(rows[0]["pid_base64"], b64)
        self.assertEqual(rows[0]["pid_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(rows[0]["name2"], "arm-2/part-1")
        self.assertEqual(rows[0]["configuration"], "SavedConfig")
        self.assertEqual(rows[0]["error_code"], 0)

    def test_failed_native_pid_binding_is_preserved_with_error(self):
        xml = "<Root><SWComponentPIDs><base64Binary>AAE=</base64Binary></SWComponentPIDs></Root>"

        def resolve(_value, errors):
            errors.value = 3
            return None

        with _variants():
            row = N._urdf_exporter_pid_bindings(
                xml, SimpleNamespace(GetObjectByPersistReference3=resolve), Path("/locked")
            )[0]
        self.assertEqual(row["pid_hex"], "0001")
        self.assertEqual(row["error_code"], 3)
        self.assertIsNone(row["name2"])
        self.assertIn("resolution_error", row)

    def test_polymorphic_return_is_not_bound_to_mate_interface(self):
        seen = []
        raw = object()
        bound = object()
        ole = SimpleNamespace(GetIDsOfNames=lambda name: 17, InvokeTypes=lambda *args: seen.append(args) or raw)
        client = SimpleNamespace(Dispatch=lambda value: bound if value is raw else None)
        with (
            _variants(),
            patch.object(N, "_win32", return_value=client),
            patch.object(N, "_dynamic", side_effect=lambda x: x),
        ):
            value = N._specific_feature_object(SimpleNamespace(_oleobj_=ole))
        self.assertIs(value, bound)
        self.assertEqual(seen, [(17, 0, 1, (13, 0), ())])


class ReferenceAxisReadTests(unittest.TestCase):
    def feature(self, endpoints=(1, 2, 3, 1, 5, 7), **kwargs):
        return _Feature("a_joint", "RefAxis", specific=SimpleNamespace(GetRefAxisParams=lambda: endpoints), **kwargs)

    def test_endpoint_geometry_is_preserved_and_derived_in_explicit_frame(self):
        for component, frame in (("", "assembly"), ("arm-1/part-2", "component_local")):
            with self.subTest(component=component):
                record = N._reference_axis_record(_doc(self.feature()), "a_joint", component)
                self.assertEqual(record["raw_endpoints_m"], [1.0, 2.0, 3.0, 1.0, 5.0, 7.0])
                self.assertEqual(record["axis_point_m"], [1.0, 2.0, 3.0])
                self.assertEqual(record["axis_direction"], [0.0, 0.6, 0.8])
                self.assertEqual(record["coordinate_frame"], frame)
                self.assertEqual(
                    record["selector"], {"kind": "reference_axis", "component": component, "feature_name": "a_joint"}
                )
                self.assertNotIn("surface", record)
                self.assertNotIn("radius_m", record)

    def test_invalid_native_axis_cannot_be_qualified(self):
        for feature in (
            None,
            self.feature((0, 0, 0, 0, 0, 0)),
            self.feature((0, 1)),
            self.feature((0, 0, 0, float("nan"), 0, 1)),
            self.feature(suppressed=True),
            self.feature(error_code=1),
        ):
            with self.subTest(feature=feature), self.assertRaises(CadError) as caught:
                N._reference_axis_record(_doc(feature), "a_joint")
            self.assertEqual(caught.exception.code, "cad_axis_reference_unreadable")

    def test_empty_component_uses_the_captured_assembly(self):
        backend = N.SolidWorksBackend()
        backend._scene_document_key = "saved-assembly"
        reference = {"kind": "reference_axis", "component": "", "feature_name": "a_joint"}
        with patch.object(backend, "_captured_document", return_value=_doc(self.feature())) as captured:
            result = backend.capture_axis_reference(reference)
        captured.assert_called_once_with("saved-assembly", phase="axis_reference")
        self.assertEqual(result["coordinate_frame"], "assembly")

    def test_missing_component_and_wrong_selector_do_not_fall_back(self):
        backend = N.SolidWorksBackend()
        for reference in (
            {"kind": "reference_axis", "component": "missing", "feature_name": "a_joint"},
            {"kind": "reference_axis", "component": "", "face_index": 0},
        ):
            with self.subTest(reference=reference), self.assertRaises(CadError):
                backend.capture_axis_reference(reference)


if __name__ == "__main__":
    unittest.main()
