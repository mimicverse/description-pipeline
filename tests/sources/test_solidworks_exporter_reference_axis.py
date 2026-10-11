"""Portable boundary checks; real native evidence is separately hash-bound."""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.sources.solidworks import native as N
from description_pipeline.sources.solidworks.errors import CadError
from tests.sources.test_solidworks_native_reader import (
    _App,
    _Component,
    _Doc,
    _Feature,
    _Session,
    _com_stubs,
    _raiser,
    _write,
)


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

        with _variants(), patch.object(N, "_relative_document", return_value="part.SLDPRT"):
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
        self.assertNotIn("resolution_error", rows[0])

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


def session_materialization(directory: Path, name: str) -> Path:
    """A structural session materialization: IC~~ under the system temp area."""

    path = directory / "swx4242" / "IC~~" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"generated native part")
    return path


class ImportedSourceBindingTests(unittest.TestCase):
    def test_pid_source_is_captured_before_shared_native_document_reads_change_import_metadata(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as native_tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            imported = _write(root, "battery.step")
            generated = session_materialization(Path(native_tmp), "battery.step.SLDPRT")
            component = _Component("battery-1", generated)
            component.imported_path = str(imported)
            component.GetImportedPath = lambda: component.imported_path
            component.IsVirtual = False
            original_get_model = component.GetModelDoc2

            def shared_document():
                component.imported_path = "/old-authoring-folder/battery.step"
                return original_get_model()

            component.GetModelDoc2 = shared_document
            xml = "<Root><SWComponentPIDs><base64Binary>AAE=</base64Binary></SWComponentPIDs></Root>"
            attribute_feature = _exporter_doc(xml).FirstFeature()
            doc = _Doc(assembly, first_feature=attribute_feature, children=[component])
            doc.Extension.GetObjectByPersistReference3 = lambda _pid, _code: component
            app = _App({assembly: doc})
            backend = N.SolidWorksBackend(session_factory=lambda: _Session(app))
            with _variants():
                record = backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
            self.assertEqual(record["exporter"]["state"], "extracted")
            row = record["exporter"]["pid_bindings"][0]
            # The cached class is a complete identity: the row's document is the
            # occurrence's own session materialization (same normalized spelling
            # as the active walk), and the author path is author metadata,
            # captured before the shared-document read could change it.
            self.assertEqual(row["document"], N.normalize_document_path(str(generated)))
            self.assertEqual(row["source_provenance"]["state"], "cached_materialization")
            self.assertEqual(row["source_provenance"]["author_imported_path"], str(imported))
            self.assertTrue(row["source_provenance"]["author_imported_in_source_root"])
            self.assertEqual(row["source_provenance"]["author_imported_relative"], "battery.step")
            self.assertNotIn("resolution_error", row)
            component_row = record["components"][0]
            self.assertEqual(row["document"], component_row["document"])
            self.assertEqual(component_row["source_provenance"]["state"], "cached_materialization")

    def test_discovery_records_staleness_without_rebuilding_the_saved_model(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly)
            doc.ForceRebuild3 = _raiser(AssertionError("saved-state discovery must never rebuild"))
            doc.GetSaveFlag = lambda: False
            doc.Extension.NeedsRebuild2 = 1
            app = _App({assembly: doc})
            backend = N.SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                record = backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
        self.assertIs(record["document_state"]["before"]["save_flag"], False)
        self.assertEqual(record["document_state"]["before"]["needs_rebuild2_value"], 1)
        self.assertIs(record["document_state"]["after"]["save_flag"], False)

    def test_discovery_records_cached_materialization_without_hashing_the_generated_temp_file(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as native_tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            imported = _write(root, "battery.step")
            generated = session_materialization(Path(native_tmp), "battery.step.SLDPRT")
            component = _Component("battery-1", generated)
            component.GetImportedPath = lambda: str(imported)
            component.IsVirtual = False
            app = _App({assembly: _Doc(assembly, children=[component])})
            backend = N.SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                record = backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
            entry = record["components"][0]
            provenance = entry["source_provenance"]
            self.assertEqual(provenance["state"], "cached_materialization")
            self.assertEqual(provenance["native_document_path"], str(generated))
            self.assertEqual(provenance["materialization_path"], str(generated))
            self.assertEqual(provenance["author_imported_path"], str(imported))
            self.assertTrue(provenance["author_imported_in_source_root"])
            self.assertEqual(provenance["author_imported_relative"], "battery.step")
            # The generated temp file is never hashed into the delivered file
            # map; the validated in-stage author source IS part of the acquired
            # inventory (its staged bytes are hashed), matching the sealed
            # selection's retained bound source.
            self.assertNotIn(str(generated), record["files"])
            self.assertEqual(
                record["files"]["battery.step"], hashlib.sha256(imported.read_bytes()).hexdigest()
            )

    def test_discovery_hashes_the_validated_in_stage_author_source(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as native_tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            imported = _write(root, "NP-F550.step")
            generated = session_materialization(Path(native_tmp), "NP-F550.step.SLDPRT")
            component = _Component("NP-F550.step-1", generated)
            component.GetImportedPath = lambda: str(imported)
            component.IsVirtual = False
            app = _App({assembly: _Doc(assembly, children=[component])})
            backend = N.SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                record = backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
            # The validated in-stage author source is an acquired CAD document:
            # its staged bytes are hashed into the native file inventory.
            self.assertEqual(
                record["files"]["NP-F550.step"], hashlib.sha256(imported.read_bytes()).hexdigest()
            )
            self.assertIn("robot.SLDASM", record["files"])

    def test_discovery_never_hashes_external_author_metadata(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as native_tmp, TemporaryDirectory() as outside:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            external = Path(outside) / "NP-F550.step"
            external.write_bytes(b"external author source")
            generated = session_materialization(Path(native_tmp), "NP-F550.step.SLDPRT")
            component = _Component("NP-F550.step-1", generated)
            component.GetImportedPath = lambda: str(external)
            component.IsVirtual = False
            app = _App({assembly: _Doc(assembly, children=[component])})
            backend = N.SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                record = backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
            self.assertNotIn("NP-F550.step", record["files"])
            for name in record["files"]:
                self.assertFalse(str(name).startswith("/"))

    def test_discovery_refuses_a_missing_validated_in_stage_source(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as native_tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            (root / "NP-F550.step").write_bytes(b"present at read time")
            generated = session_materialization(Path(native_tmp), "NP-F550.step.SLDPRT")
            component = _Component("NP-F550.step-1", generated)
            component.GetImportedPath = lambda: str(root / "NP-F550.step")
            component.IsVirtual = False
            app = _App({assembly: _Doc(assembly, children=[component])})
            backend = N.SolidWorksBackend(session_factory=lambda: _Session(app))
            real = N._component_source_document

            def forged(component_arg, source_root):
                result = real(component_arg, source_root)
                provenance = result.get("source_provenance") or {}
                if provenance.get("state") == "cached_materialization":
                    provenance["author_imported_in_source_root"] = True
                    provenance["author_imported_relative"] = "missing-under-root.step"
                return result

            with _com_stubs(), patch.object(N, "_component_source_document", side_effect=forged):
                with self.assertRaises(CadError) as caught:
                    backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
            self.assertEqual(caught.exception.code, "cad_import_source_unavailable")

    def test_current_import_api_records_cached_materialization_with_in_stage_source(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as native_tmp:
            root = Path(tmp)
            source = root / "battery.step"
            source.write_bytes(b"locked imported source")
            generated = session_materialization(Path(native_tmp), "battery.step.SLDPRT")
            component = SimpleNamespace(
                GetPathName=lambda: str(generated),
                GetImportedPath=lambda: str(source),
                IsVirtual=False,
            )
            result = N._component_source_document(component, root)
            self.assertEqual(result["source_provenance"]["source_root"], str(root.resolve()))
            self.assertIsNone(result["document"])
            provenance = result["source_provenance"]
            self.assertEqual(provenance["state"], "cached_materialization")
            self.assertEqual(provenance["materialization_path"], str(generated))
            self.assertEqual(provenance["session_temp_root"], str(Path(native_tmp) / "swx4242"))
            self.assertTrue(provenance["author_imported_in_source_root"])
            self.assertEqual(provenance["author_imported_relative"], "battery.step")
            self.assertIs(provenance["is_virtual"], False)

    def test_matching_basename_never_substitutes_for_an_external_import(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "battery.step").write_bytes(b"unrelated same name")
            component = SimpleNamespace(
                GetPathName=lambda: "/temporary/current-session/battery.step.SLDPRT",
                GetImportedPath=lambda: "/outside/battery.step",
                IsVirtual=False,
            )
            result = N._component_source_document(component, root)
        self.assertIsNone(result["document"])
        self.assertEqual(result["source_provenance"]["state"], "unread")
        error = result["source_provenance"]["error"]
        self.assertIn("not a structural session materialization", error)
        self.assertIn("IC~~", error)


if __name__ == "__main__":
    unittest.main()
