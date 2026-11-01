"""Typed in-place observations and independent declaration-scope decisions."""

from __future__ import annotations

import copy
import unittest
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET
from types import SimpleNamespace
from unittest import mock

from description_pipeline.sources.solidworks import discovery, native
from description_pipeline.verification import native_discovery
from description_pipeline.verification.native_declarations import verify_declarations

from . import support
from .selection_support import seal_fixture_object_observation
from .test_solidworks_discovery import FakeMateFeature
from .test_verify_declarations import fixture, rewrite, NS_TAG, Z_TAG


def observed_mate(scope="module-1"):
    """Exercise the actual reader with nullable and failed native properties."""

    feature = SimpleNamespace(Name="placement", IsSuppressed=lambda: False, GetErrorCode=lambda: 0)
    specific = SimpleNamespace(Component=None)

    def indexed(_specific, member, index):
        if member == "MateEntityType":
            return 4
        if member == "MateEntity":
            return None
        if member == "MateComponentName" and index == 0:
            return "tool_part"
        raise native.CadError("cad_member_missing", "The native indexed property is unavailable", {"index": index})

    with mock.patch.object(native, "_indexed_property", side_effect=indexed):
        return native._inplace_mate_record(feature, specific, 2, scope=scope, configuration="Default")


def native_record(mate):
    raw = fixture()
    raw["identity"]["delivery_configuration"] = "Default"
    documents = {item["document"] for item in raw["components"]} | {"robot.SLDASM"}
    raw["files"] = dict.fromkeys(sorted(documents), "f" * 64)
    def namespace_types(root):
        root.set("xmlns", NS_TAG[1:-1])
        root.set("xmlns:b", "http://www.w3.org/2001/XMLSchema")
        root.set("xmlns:a", "http://schemas.microsoft.com/2003/10/Serialization/Arrays")
        for link in root.iter(NS_TAG + "Link"):
            if link.find(NS_TAG + "isIncomplete") is None:
                ET.SubElement(link, NS_TAG + "isIncomplete").text = "false"
            if link.find(NS_TAG + "SWMainComponentPID") is None:
                ET.SubElement(link, NS_TAG + "SWMainComponentPID").set(
                    "{http://www.w3.org/2001/XMLSchema-instance}nil", "true"
                )
        for attribute in root.iter(NS_TAG + "Attribute"):
            if attribute.get(Z_TAG + "Id") == "joint_limit_VelocityAttribute":
                attribute.find(NS_TAG + "Value").set(
                    "{http://www.w3.org/2001/XMLSchema-instance}type", "b:double"
                )

    rewrite(raw, namespace_types)
    raw = support.local_native_record_payload(raw, default_mate_scope=True)
    raw["mates"].append(copy.deepcopy(mate))
    return raw


def sealed_native_record(mate):
    return seal_fixture_object_observation(native_record(mate), main_assembly="robot.SLDASM")


class InPlaceNativeTests(unittest.TestCase):
    def test_reference_plane_retains_geometry_component_and_unavailable_type_info(self):
        component = SimpleNamespace(
            Name2="tool_part", ReferencedConfiguration="Default", GetPathName=lambda: "part.SLDPRT"
        )
        failure = RuntimeError("Type information is unavailable")
        failure.hresult = -2147352565
        plane = SimpleNamespace(
            _oleobj_=SimpleNamespace(GetTypeInfo=mock.Mock(side_effect=failure)),
            GetComponent=lambda: component,
            Transform=SimpleNamespace(ArrayData=tuple(range(16))),
        )
        with mock.patch.object(native, "_interface", side_effect=lambda value, _iid: value):
            record = native._inplace_entity(plane, 4)
        self.assertEqual(record["interface_iid"]["error"]["hresult"], -2147352565)
        self.assertEqual(record["component"]["value"]["name2"]["value"], "tool_part")
        self.assertEqual(record["transform_raw"], {"value": list(range(16)), "error": None})

    def test_actual_specific_interface_is_selected_by_native_feature_type(self):
        with mock.patch.object(native, "_interface", side_effect=lambda value, _iid: value) as bind:
            _specific, count = native._mate_specific(FakeMateFeature("MateInPlace", count=2), strict=True)
        self.assertEqual(count, 2)
        self.assertEqual(bind.call_args.args[1], native.IMATEINPLACE_IID)

    def test_null_specific_interface_is_refused(self):
        with (
            mock.patch.object(native, "_specific_feature_object", return_value=None),
            self.assertRaises(native.CadError),
        ):
            native._mate_specific(FakeMateFeature("MateInPlace", count=2), strict=True)

    def test_null_component_and_partial_endpoints_remain_original_observations(self):
        mate = observed_mate()
        self.assertEqual(mate["type"], "inplace")
        self.assertNotIn("alignment", mate)
        self.assertNotIn("type_index", mate)
        self.assertEqual(mate["entities"], [])
        self.assertEqual(mate["inplace"]["component"], {"value": None, "error": None})
        self.assertEqual(mate["inplace"]["entities"][0]["entity"], {"value": None, "error": None})
        error = mate["inplace"]["entities"][1]["component_name"]["error"]
        self.assertEqual(error["code"], "cad_member_missing")
        self.assertEqual(error["detail"], {"index": 1})

    def test_indexed_scalar_properties_never_become_com_interface_queries(self):
        for value in ("component", 4, 0, False):
            ole = SimpleNamespace(GetIDsOfNames=lambda _name: 17, Invoke=lambda *_args, result=value: result)
            with self.subTest(value=value), mock.patch.dict(
                "sys.modules", {"pythoncom": SimpleNamespace(DISPATCH_PROPERTYGET=2)}
            ), mock.patch.object(native, "_win32", side_effect=AssertionError("a scalar is not a COM object")):
                self.assertEqual(native._indexed_property(SimpleNamespace(_oleobj_=ole), "MateEntityType", 0), value)


class InPlaceConsumerTests(unittest.TestCase):
    def source(self, raw):
        projected = discovery._project_native_record(raw)
        findings = []
        declaration = discovery._exporter_declaration(projected, projected["identity"], findings)
        self.assertIsNotNone(declaration, findings)
        discovery._exporter_memberships(projected, declaration, findings)
        return projected, findings

    def verify(self, raw):
        return verify_declarations(native_discovery.project_native_verification_record(raw))

    def test_partial_internal_mate_preserves_warning_without_constraint_rows(self):
        raw = sealed_native_record(observed_mate())
        original = copy.deepcopy(raw)
        projected, findings = self.source(raw)
        finding = next(item for item in findings if item["code"] == "discovery.inplace_constraint_unmeasured")
        self.assertFalse(finding["blocking"])
        self.assertEqual(finding["detail"]["declared_links"], ["tool"])
        self.assertEqual(finding["detail"]["native_observations"], raw["mates"][-1]["inplace"])
        self.assertIsNone(native_discovery._rows_for(projected["mates"][-1], {}))
        report = self.verify(raw)
        self.assertTrue(report["passed"], report["errors"])
        warning = next(item for item in report["diagnostics"] if item["code"] == "v14.inplace_constraint_unmeasured")
        self.assertEqual(warning["detail"]["declared_link"], "tool")
        self.assertEqual(warning["detail"]["native_observations"], raw["mates"][-1]["inplace"])
        self.assertEqual(raw, original)

    def test_raw_observation_warning_is_verified_and_present_in_actual_urdf(self):
        from description_pipeline.backends.urdf import generate_urdf
        from description_pipeline.io import PipelineError
        from description_pipeline.sources.solidworks.evidence import inplace_observation_warnings
        from description_pipeline.verification.actual_urdf import _warning_comments
        from description_pipeline.verification.solidworks_urdf import _value_warning_gate
        from .test_solidworks_value_warnings import ValueWarningTests

        raw = sealed_native_record(observed_mate())
        warning = inplace_observation_warnings(raw)
        self.assertEqual(warning[0]["objects"][0]["actual"], raw["mates"][-1]["inplace"])
        self.assertFalse(warning[0]["objects"][0]["constraint_rows_used"])
        comments = _warning_comments({"provenance": {"value_warnings": warning}})
        self.assertIn("source.inplace_observations", comments[0])
        self.assertIn('"value":null', comments[0])
        self.assertIn("cad_member_missing", comments[0])
        model = {"links": [], "joints": [], "frames": [], "provenance": {"value_warnings": warning}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = generate_urdf(SimpleNamespace(name="fixture", to_dict=lambda: model), root, root)
            self.assertIn(comments[0], output.read_text())
        scenario = ValueWarningTests(methodName="runTest")
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        scenario.model["provenance"]["value_warnings"].extend(warning)
        _value_warning_gate(scenario.root, scenario.source, scenario.model, observation_warnings=warning)
        forged = copy.deepcopy(scenario.model)
        forged["provenance"]["value_warnings"][-1]["objects"][0]["actual"]["entities"].pop()
        with self.assertRaises(PipelineError):
            _value_warning_gate(scenario.root, scenario.source, forged, observation_warnings=warning)

    def test_unmeasured_cross_link_scope_blocks_both_consumers(self):
        raw = sealed_native_record(observed_mate(scope=""))
        _projected, findings = self.source(raw)
        self.assertTrue(any(
            row["blocking"] for row in findings if row["code"] == "discovery.inplace_constraint_unmeasured"
        ))
        report = self.verify(raw)
        self.assertFalse(report["passed"])
        self.assertIn("v14.inplace_scope", {row["code"] for row in report["errors"]})

    def test_observed_component_binding_must_belong_to_owner_scope_and_configuration(self):
        for name, configuration, allowed in (
            ("tool_part", "Default", True),
            ("module-1/tool_part", "Default", True),
            ("base_part", "Default", False),
            ("tool_part", "Other", False),
        ):
            mate = observed_mate()
            mate["inplace"]["component"]["value"] = {
                "name2": {"value": name, "error": None},
                "path": {"value": None, "error": None},
                "configuration": {"value": configuration, "error": None},
            }
            raw = sealed_native_record(mate)
            with self.subTest(name=name, configuration=configuration):
                if allowed:
                    self.source(raw)
                    report = self.verify(raw)
                    self.assertTrue(report["passed"], report["errors"])
                else:
                    with self.assertRaises(native.CadError):
                        self.source(raw)
                    report = self.verify(raw)
                    self.assertFalse(report["passed"])
                    self.assertIn("v14.inplace_binding", {row["code"] for row in report["errors"]})

    def test_unassigned_material_cannot_be_completed_by_inplace(self):
        raw = sealed_native_record(observed_mate())

        def remove_container_selection(root):
            root.set("xmlns", NS_TAG[1:-1])
            root.set("xmlns:b", "http://www.w3.org/2001/XMLSchema")
            root.set("xmlns:a", "http://schemas.microsoft.com/2003/10/Serialization/Arrays")
            for link in root.iter(NS_TAG + "Link"):
                if link.get(Z_TAG + "Id") == "tool":
                    pids = link.find(NS_TAG + "SWComponentPIDs")
                    pids.remove(next(iter(pids)))

        rewrite(raw, remove_container_selection)
        raw["exporter"]["pid_bindings"] = [row for row in raw["exporter"]["pid_bindings"] if row["pid_hex"] != "414243"]
        _projected, findings = self.source(raw)
        self.assertTrue(any(
            row["blocking"] for row in findings if row["code"] == "discovery.inplace_constraint_unmeasured"
        ))
        report = self.verify(raw)
        self.assertFalse(report["passed"])
        self.assertIn("v14.inplace_scope", {row["code"] for row in report["errors"]})

    def test_readable_component_path_conflict_is_not_hidden_by_same_name_and_configuration(self):
        from description_pipeline.sources.solidworks.session_artifacts import SESSION_DOCUMENT_API

        for path, allowed in (("c:/input/module-1_tool_part.SLDPRT", True), ("C:/other/part.SLDPRT", False)):
            raw = sealed_native_record(observed_mate())
            component = next(row for row in raw["components"] if row["name2"] == "module-1/tool_part")
            provenance = {
                "state": "resolved", "kind": "native_imported_document", "used_api": SESSION_DOCUMENT_API,
                "document": component["document"], "sha256": raw["files"][component["document"]],
                "is_virtual": False, "source_root": "C:/input", "imported_path": "C:/input/" + component["document"],
                "native_document_path": "C:/input/" + component["document"],
            }
            component["source_provenance"] = provenance
            for row in raw["exporter"]["pid_bindings"]:
                if row["name2"] == component["name2"]:
                    row["source_provenance"] = copy.deepcopy(provenance)
            raw["mates"][-1]["inplace"]["component"]["value"] = {
                "name2": {"value": "tool_part", "error": None},
                "path": {"value": path, "error": None},
                "configuration": {"value": "Default", "error": None},
            }
            with self.subTest(path=path):
                if allowed:
                    self.source(raw)
                    report = self.verify(raw)
                    self.assertTrue(report["passed"], report["errors"])
                else:
                    with self.assertRaises(native.CadError):
                        self.source(raw)
                    report = self.verify(raw)
                    self.assertIn("v14.inplace_binding", {row["code"] for row in report["errors"]})

    def test_solver_mode_rejects_unmeasured_inplace_instead_of_ignoring_it(self):
        raw = discovery._project_native_record(sealed_native_record(observed_mate()))
        raw.pop("exporter")
        findings = []
        discovery._clusters(raw, findings)
        self.assertTrue(any(row["code"] == "discovery.mate_unsupported" and row["blocking"] for row in findings))

    def test_suppressed_relationship_is_retained_without_active_scope_claim(self):
        mate = observed_mate(scope="")
        mate["suppressed"] = True
        raw = sealed_native_record(mate)
        _projected, findings = self.source(raw)
        self.assertFalse(any(row["code"] == "discovery.inplace_constraint_unmeasured" for row in findings))
        self.assertTrue(self.verify(raw)["passed"])

    def test_malformed_inplace_records_are_refused_by_both_projectors(self):
        mutations = [
            lambda mate: mate.update(type="lock"),
            lambda mate: mate.update(alignment=0),
            lambda mate: mate.update(entity_count=True),
            lambda mate: mate["inplace"]["entities"][0]["entity_type"].update(value=True),
            lambda mate: mate["inplace"]["entities"][1]["component_name"].update(value="forged"),
            lambda mate: mate["inplace"]["entities"][0].pop("entity"),
        ]
        for index, mutate in enumerate(mutations):
            mate = observed_mate()
            mutate(mate)
            raw = sealed_native_record(mate)
            for projector in (discovery._project_native_record, native_discovery.project_native_verification_record):
                with self.subTest(index=index, projector=projector.__name__), self.assertRaises(
                    (native.CadError, native_discovery._Failure)
                ):
                    projector(raw)

    def test_typed_entity_fields_cannot_be_truncated_or_forged(self):
        for kind, field, count in ((2, "plane_params", 6), (4, "transform_raw", 16)):
            for value, allowed in (("missing", False), ([0.0, 1.0], False), ([0.0] * count, True), (None, True)):
                mate = observed_mate()
                endpoint = mate["inplace"]["entities"][0]
                endpoint["entity_type"]["value"] = kind
                geometry = {
                    "interface_iid": {"value": None, "error": {"code": "unavailable", "message": "No type info"}},
                    "component": {"value": None, "error": None},
                }
                if value != "missing":
                    geometry[field] = {"value": value, "error": None}
                endpoint["entity"] = {"value": geometry, "error": None}
                raw = sealed_native_record(mate)
                for projector in (
                    discovery._project_native_record, native_discovery.project_native_verification_record
                ):
                    with self.subTest(kind=kind, value=value, projector=projector.__name__):
                        if allowed:
                            projector(raw)
                        else:
                            with self.assertRaises((native.CadError, native_discovery._Failure)):
                                projector(raw)
