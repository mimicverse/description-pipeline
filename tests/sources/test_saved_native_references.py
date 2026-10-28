"""Saved native inspection declarations bind by context, topology and geometry."""

from __future__ import annotations

import copy
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
        datums.append({"name": f"c_{name}", "owner": "", "configuration": "Default", "array": matrix})
        axes.append({"kind": "reference_axis", "component": "", "coordinate_frame": "assembly",
                     "direction_semantics": "undirected_axis_line", "axis_point_m": [x, 0.0, 0.0],
                     "axis_direction": [0.0, 0.0, 1.0], "raw_endpoints_m": [x, 0.0, 0.0, x, 0.0, 1.0],
                     "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
                     "selector": {"kind": "reference_axis", "component": "", "feature_name": f"a_{name}"}})
    mate = {"name": "bearing", "scope": "", "configuration": "Default", "suppressed": False,
            "type": "concentric", "error_code": 0, "entity_count": 2, "lock_rotation": False,
            "limits": None, "native_limit": None, "is_advanced_mate": None,
            "is_advanced_mate_error": None, "is_advanced_mate_api": None,
            "entities": [{
                "component": f"{name}-1/part-1", "assembly_frame": False,
                "mate_entity_reference": {"source": "mate-entity-params", "space": "mate-assembly",
                                          "reference_type": 4, "params": [0.5, 0.0, 0.0, 0.0, 0.0, 1.0, 0.01],
                                          "error": None},
            } for name in ("base", "moving")]}
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
            "boolean_rigid": lambda r: r["components"][2]["solver_observation"]["solving"].update(value=False),
            "foreign_context": lambda r: r["components"][3]["solver_observation"]["context"].update(document="other"),
            "duplicate_root": lambda r: r["components"][2].update(fixed=True),
            "unknown_root": lambda r: r["components"][2].update(fixed=None),
            "partial_pair": lambda r: r["reference_axes"].pop(),
            "duplicate_datum": lambda r: r["datums"].append(copy.deepcopy(r["datums"][-1])),
            "foreign_axis_scope": lambda r: r["reference_axes"][-1].update(component="moving-1"),
            "zero_axis": lambda r: r["reference_axes"][-1].update(raw_endpoints_m=[0.0] * 6),
            "different_line": lambda r: r["mates"][0]["entities"][0]["mate_entity_reference"]["params"].__setitem__(
                0, 1.0,
            ),
            "native_error": lambda r: r["mates"][0].update(error_code=1),
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
