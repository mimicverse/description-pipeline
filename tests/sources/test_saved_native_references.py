"""Saved native inspection declarations bind by context, topology and geometry."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from description_pipeline.sources.solidworks.discovery import DiscoverySettings, _saved_native_carriers
from description_pipeline.sources.solidworks.saved_native_references import (
    SavedNativeReferenceError,
    bind_saved_native_references,
)


def inspection_record():
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    components = []
    for name in ("base", "moving"):
        for leaf in (False, True):
            occurrence = f"{name}-1" + ("/part-1" if leaf else "")
            components.append({
                "name2": occurrence, "document": f"{name}.SLDPRT" if leaf else f"{name}.SLDASM",
                "parent_occurrence": f"{name}-1" if leaf else "", "configuration": "Default",
                "suppressed": False, "fixed": not leaf and name == "base", "fixed_error": None,
                "transform": list(identity),
                "solver_observation": {
                    "context": {"document": f"{name}.SLDASM" if leaf else "machine.SLDASM",
                                "configuration": "Default", "scope": f"{name}-1" if leaf else ""},
                    "solving": {"method": "IComponent2.Solving", "family": "swComponentSolvingOption_e",
                                "value": 0, "error": None},
                },
            })
    datums, axes = [], []
    for name, x in (("base_link", 0.0), ("wrist", 0.5)):
        matrix = list(identity)
        matrix[3] = x
        datums.append({"name": f"c_{name}", "owner": "", "configuration": "Default", "array": matrix,
                       "references": {"context": {"document": "machine.SLDASM",
                                                  "configuration": "Default", "scope": ""}}})
        axes.append({"kind": "reference_axis", "component": "", "coordinate_frame": "assembly",
                     "direction_semantics": "undirected_axis_line", "axis_point_m": [x, 0.0, 0.0],
                     "axis_direction": [0.0, 0.0, 1.0], "raw_endpoints_m": [x, 0.0, 0.0, x, 0.0, 1.0],
                     "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
                     "selector": {"kind": "reference_axis", "component": "", "feature_name": f"a_{name}"}})
    mate = {"name": "bearing", "scope": "", "configuration": "Default", "suppressed": False,
            "type": "concentric", "error_code": 0, "entity_count": 2, "lock_rotation": False,
            "lock_rotation_api": "IConcentricMateFeatureData.LockRotation",
            "limits": None, "native_limit": None, "is_advanced_mate": None,
            "is_advanced_mate_error": None, "is_advanced_mate_api": None,
            "entities": [{
                "component": f"{name}-1/part-1", "assembly_frame": False, "entity_index": index, "feature": "bearing",
                "mate_entity_reference": {"source": "mate-entity-params", "space": "mate-assembly",
                                          "reference_type": 4, "params": [0.5, 0.0, 0.0, 0.0, 0.0, 1.0, 0.01],
                                          "error": None},
            } for index, name in enumerate(("base", "moving"))]}
    return {"identity": {"main_assembly": "machine.SLDASM"}, "saved_configuration": "Default",
            "exporter": {"state": "absent", "document": "machine.SLDASM", "configuration": "Default"},
            "components": components, "datums": datums, "reference_axes": axes, "mates": [mate],
            "properties": {"document": {}, "components": {}, "mates": {}}}


class SavedNativeReferencesTests(unittest.TestCase):
    def test_unrelated_native_names_bind_by_the_current_interface_geometry(self):
        raw = inspection_record()
        original = copy.deepcopy(raw)
        proof = bind_saved_native_references(raw, purpose="inspection_preview")
        self.assertEqual(proof["features"], {"base-1": "base_link", "moving-1": "wrist"})
        self.assertEqual(proof["parent"], {"base-1": None, "moving-1": "base-1"})
        raw["components"].reverse()
        raw["datums"].reverse()
        raw["reference_axes"].reverse()
        self.assertEqual(bind_saved_native_references(raw, purpose="inspection_preview")["features"], proof["features"])
        raw["components"].reverse()
        raw["datums"].reverse()
        raw["reference_axes"].reverse()
        self.assertEqual(raw, original)

    def test_native_context_and_geometric_conflicts_cannot_become_warnings(self):
        cases = {
            "flexible": lambda r: r["components"][2]["solver_observation"]["solving"].update(value=1),
            "unknown_exporter": lambda r: r.pop("exporter"),
            "missing_component_document": lambda r: r["components"][2].pop("document"),
            "missing_datum_name": lambda r: r["datums"][-1].pop("name"),
            "foreign_exporter_context": lambda r: r["exporter"].update(configuration="Other"),
            "conflicting_registration": lambda r: r["exporter"].update(registration={"state": "present"}),
            "exporter_read_error": lambda r: r["exporter"].update(registration_error="unread"),
            "boolean_rigid": lambda r: r["components"][2]["solver_observation"]["solving"].update(value=False),
            "foreign_context": lambda r: r["components"][3]["solver_observation"]["context"].update(document="other"),
            "duplicate_root": lambda r: r["components"][2].update(fixed=True),
            "unknown_root": lambda r: r["components"][2].update(fixed=None),
            "partial_pair": lambda r: r["reference_axes"].pop(),
            "duplicate_datum": lambda r: r["datums"].append(copy.deepcopy(r["datums"][-1])),
            "datum_context": lambda r: r["datums"][-1].update(context={"document": "other.SLDASM"}),
            "datum_error": lambda r: r["datums"][-1].update(error="unread"),
            "foreign_axis_scope": lambda r: r["reference_axes"][-1].update(component="moving-1"),
            "axis_configuration": lambda r: r["reference_axes"][-1].update(configuration="Other"),
            "axis_context": lambda r: r["reference_axes"][-1].update(context={"document": "other.SLDASM"}),
            "axis_error": lambda r: r["reference_axes"][-1].update(error="unread"),
            "zero_axis": lambda r: r["reference_axes"][-1].update(raw_endpoints_m=[0.0] * 6),
            "different_line": lambda r: r["mates"][0]["entities"][0]["mate_entity_reference"]["params"].__setitem__(
                0, 1.0,
            ),
            "native_error": lambda r: r["mates"][0].update(error_code=1),
            "missing_mate_name": lambda r: r["mates"][0].pop("name"),
            "duplicate_mate_identity": lambda r: r["mates"].append(copy.deepcopy(r["mates"][0])),
            "missing_lock_api": lambda r: r["mates"][0].pop("lock_rotation_api"),
            "missing_endpoint_selector": lambda r: r["mates"][0]["entities"][0].pop("feature"),
            "duplicate_endpoint_index": lambda r: r["mates"][0]["entities"][1].update(entity_index=0),
            "locked_rotation": lambda r: r["mates"][0].update(lock_rotation=True),
            "unsupported_endpoint": lambda r: r["mates"][0]["entities"][0]["mate_entity_reference"].update(
                reference_type=0,
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                raw = inspection_record()
                mutate(raw)
                original = copy.deepcopy(raw)
                with self.assertRaises(SavedNativeReferenceError):
                    bind_saved_native_references(raw, purpose="inspection_preview")
                self.assertEqual(raw, original)

    def test_saved_carriers_supply_only_a_warned_inspection_window(self):
        findings = []
        bodies, joints, frames, _names = _saved_native_carriers(
            inspection_record(), DiscoverySettings(purpose="inspection_preview"), findings,
        )
        self.assertFalse([row for row in findings if row["blocking"]], findings)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(len(joints), 1)
        self.assertEqual(frames, [])
        self.assertIsNone(joints[0]["preview_policy"]["author_limits"])
        self.assertFalse(joints[0]["preview_policy"]["native_cad_ranges"])
        self.assertFalse(joints[0]["preview_policy"]["hardware_qualified"])
        self.assertIn("direction.preview_endpoint_order", {row["code"] for row in findings})
        with self.assertRaises(SavedNativeReferenceError):
            bind_saved_native_references(inspection_record(), purpose="engineering")

    def test_nested_assembly_requires_its_own_rigid_observation(self):
        raw = inspection_record()
        inner = copy.deepcopy(raw["components"][-1])
        inner.update(name2="moving-1/inner-1", document="inner.SLDASM")
        leaf = raw["components"][-1]
        leaf.update(name2="moving-1/inner-1/part-1", parent_occurrence=inner["name2"])
        leaf["solver_observation"]["context"].update(document="inner.SLDASM", scope=inner["name2"])
        raw["components"].append(inner)
        raw["mates"][0]["entities"][-1]["component"] = leaf["name2"]
        bind_saved_native_references(raw, purpose="inspection_preview")
        for value in (1, None, False):
            with self.subTest(value=value):
                inner["solver_observation"]["solving"]["value"] = value
                with self.assertRaises(SavedNativeReferenceError):
                    bind_saved_native_references(raw, purpose="inspection_preview")

    def test_inplace_observation_is_retained_without_generic_endpoint_or_rank_claims(self):
        from .test_inplace_mates import observed_mate

        raw = inspection_record()
        raw["mates"].append(observed_mate(scope="moving-1"))
        proof = bind_saved_native_references(raw, purpose="inspection_preview")
        self.assertEqual(proof["pose_mates"][0]["owners"], ["moving-1"])
        raw["mates"][-1]["scope"] = "other-1"
        with self.assertRaises(SavedNativeReferenceError):
            bind_saved_native_references(raw, purpose="inspection_preview")

    def test_unformattable_feature_name_has_a_structured_failure(self):
        raw = inspection_record()
        raw["datums"][-1]["name"] = "c_\u2603"
        raw["reference_axes"][-1]["selector"]["feature_name"] = "a_\u2603"
        with self.assertRaises(SavedNativeReferenceError) as caught:
            bind_saved_native_references(raw, purpose="inspection_preview")
        self.assertIn("native_name", caught.exception.details)

    def test_numbered_auto_names_cannot_disambiguate_coincident_geometry(self):
        raw = inspection_record()
        root, leaf = raw["components"][2:]
        root.update(name2="auto_link_001-1", document="auto_link_001.SLDASM")
        leaf.update(name2="auto_link_001-1/part-1", parent_occurrence=root["name2"])
        leaf["solver_observation"]["context"].update(document=root["document"], scope=root["name2"])
        raw["mates"][0]["entities"][1]["component"] = leaf["name2"]
        other_root, other_leaf = copy.deepcopy((root, leaf))
        other_root.update(name2="auto_link_002-1", document="auto_link_002.SLDASM")
        other_leaf.update(name2="auto_link_002-1/part-1", parent_occurrence=other_root["name2"])
        other_leaf["solver_observation"]["context"].update(document=other_root["document"], scope=other_root["name2"])
        raw["components"].extend((other_root, other_leaf))
        raw["datums"][-1]["name"] = "c_auto_link_001"
        raw["reference_axes"][-1]["selector"]["feature_name"] = "a_auto_link_001"
        datum, axis, mate = copy.deepcopy((raw["datums"][-1], raw["reference_axes"][-1], raw["mates"][0]))
        datum["name"] = "c_auto_link_002"
        axis["selector"]["feature_name"] = "a_auto_link_002"
        mate["name"] = "second_native_bearing"
        mate["entities"][1]["component"] = other_leaf["name2"]
        raw["datums"].append(datum)
        raw["reference_axes"].append(axis)
        raw["mates"].append(mate)
        with self.assertRaises(SavedNativeReferenceError):
            bind_saved_native_references(raw, purpose="inspection_preview")
        datum["array"][3] = 1.0
        axis["axis_point_m"][0] = 1.0
        axis["raw_endpoints_m"][0] = axis["raw_endpoints_m"][3] = 1.0
        for entity in mate["entities"]:
            entity["mate_entity_reference"]["params"][0] = 1.0
        proof = bind_saved_native_references(raw, purpose="inspection_preview")
        self.assertEqual(proof["features"][other_root["name2"]], "auto_link_002")

    def test_native_sign_warning_preserves_its_property_without_positive_motion_claim(self):
        from description_pipeline.sources.solidworks.evidence import saved_native_reference_warnings

        raw = inspection_record()
        raw["properties"]["document"]["dp.joint.wrist.axis_sign"] = -1
        findings = []
        _saved_native_carriers(raw, DiscoverySettings(purpose="inspection_preview"), findings)
        self.assertFalse([row for row in findings if row["blocking"]], findings)
        sign = next(row for row in findings if row["code"] == "direction.native_axis_sign")["detail"]
        self.assertEqual(sign["rule_id"], "native_axis_sign/v1")
        self.assertTrue(sign["native_axis_sign_present"])
        self.assertFalse(sign["native_positive_motion"])
        objects = saved_native_reference_warnings(raw, purpose="inspection_preview")[0]["objects"]
        property_warning = next(row for row in objects
                                if row["object_type"] == "native_property")
        self.assertEqual(property_warning["original"], -1)
        self.assertEqual(property_warning["rule_id"], "native_axis_sign/v1")

    def test_inspection_warning_keeps_native_field_locators_and_qualification_limits(self):
        from description_pipeline.sources.solidworks.evidence import saved_native_reference_warnings

        raw = inspection_record()
        original = copy.deepcopy(raw)
        warning = saved_native_reference_warnings(raw, purpose="inspection_preview")[0]
        self.assertEqual(warning["code"], "source.saved_native_inspection")
        self.assertEqual(
            {row["object_type"] for row in warning["objects"]},
            {"component", "coordinate_system", "reference_axis", "mate"},
        )
        self.assertTrue(all(row["source"].startswith("input/discovery/native-discovery.json#raw.")
                            for row in warning["objects"]))
        self.assertEqual(saved_native_reference_warnings(raw, purpose="engineering"), [])
        self.assertEqual(raw, original)

    def test_controlled_zero_range_and_drive_are_preserved_instead_of_defaulted(self):
        raw = inspection_record()
        raw["properties"]["document"].update({
            "dp.joint.wrist.limits_record": "joint.json",
            "dp.joint.wrist.drive_record": "joint.json",
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "joint.json").write_text(json.dumps({"lower": 0.0, "upper": 0.0,
                                                        "effort": 0.0, "velocity": 0.0}))
            findings = []
            _bodies, joints, _frames, _names = _saved_native_carriers(
                raw, DiscoverySettings(purpose="inspection_preview", record_roots=(root,)), findings,
            )
            self.assertFalse([row for row in findings if row["blocking"]], findings)
            self.assertEqual(joints[0]["limits"], {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0})
            self.assertIsNone(joints[0]["preview_policy"])
            self.assertEqual(joints[0]["limit_source"], "declared_author")
            self.assertIn("limit_evidence", joints[0])
            self.assertIn("drive", joints[0])

    def test_unreadable_controlled_range_cannot_fall_back_to_an_inspection_window(self):
        raw = inspection_record()
        raw["properties"]["document"]["dp.joint.wrist.limits_record"] = "missing.json"
        findings = []
        _saved_native_carriers(raw, DiscoverySettings(purpose="inspection_preview"), findings)
        self.assertIn("discovery.record_missing", {row["code"] for row in findings if row["blocking"]})

    def test_source_definition_keeps_drive_values_from_the_position_record(self):
        from . import support
        from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
        from description_pipeline.sources.solidworks.discovery import source_definition

        raw = inspection_record()
        raw["properties"]["document"]["dp.joint.wrist.limits_record"] = "joint.json#values"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cad = root / "cad"
            cad.mkdir()
            names = {row["document"] for row in raw["components"]} | {"machine.SLDASM"}
            for name in names:
                (cad / name).write_bytes(b"frozen native fixture")
            raw["files"] = {name: hashlib.sha256((cad / name).read_bytes()).hexdigest() for name in names}
            for component in raw["components"]:
                component.update(suppression_state=2, lightweight=False)
            raw = support.local_native_record_payload(raw, default_mate_scope=True, fill_missing_parents=True)
            package = root / "delivery"
            support.compose_delivery_inputs(
                package, main_assembly="machine.SLDASM", configuration="Default", record=raw,
                cad_files={name: cad / name for name in names},
                records={"joint.json": json.dumps({"values": {"lower": 0, "upper": 0, "effort": 2, "velocity": 3}})},
                record_keys={"joint.json": "values"},
            )
            request = build_request_evidence(
                purpose="inspection_preview", request_id="fixture-run", target_hardware="fixture",
                change_summary="fixture", main_assembly="machine.SLDASM",
                handoff_digest="0" * 64, platform_config_digest="0" * 64,
            )
            (package / "input/request.json").write_bytes(request_evidence_bytes(request))
            source = source_definition(
                package, discovery_name="input/discovery/native-discovery.json",
                cad_prefix="input/cad/", records_prefix="input/",
            )
            joint = source["joints"][0]
            self.assertEqual(joint["limits"], {"lower": 0, "upper": 0, "effort": 2, "velocity": 3})
            self.assertEqual(joint["drive_source"], "controlled")
            self.assertEqual(joint["drive_evidence"]["file"], joint["limit_evidence"]["file"])
            self.assertNotIn("preview_policy", joint)
