"""Freeze-side direct link group selection composer and evidence record.

Covers the producer composition (top-most providers + uncovered leaves) and the
fail-closed contract of the raw record written to
``raw/link_group_mass_properties.json``.
"""

from __future__ import annotations

import types
import unittest
from typing import ClassVar

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import BridgeError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import (  # noqa: E402
    _link_group_record_from_payload,
    _link_group_selections,
)

IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def _component(name: str):
    return types.SimpleNamespace(name=name, transform=list(IDENTITY))


def _scene(names, masses=None):
    return types.SimpleNamespace(
        components=[_component(name) for name in names],
        mass_properties=dict(masses or {}),
    )


def _cfg(*bodies):
    return {"groups": [{"name": name, "components": list(members)} for name, members in bodies]}


def _read(mass=1.0):
    return {
        "mass_kg": mass,
        "volume_m3": None,
        "density_kg_m3": None,
        "com_m": [0.0, 0.0, 0.0],
        "inertia_tensor_about_com": [[1e-6, 0.0, 0.0], [0.0, 1e-6, 0.0], [0.0, 0.0, 1e-6]],
        "inertia_values_about_origin_api": {"error": "fixture"},
        "overrides": {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
    }


class LinkGroupSelectionTests(unittest.TestCase):
    LEAVES: ClassVar[list[str]] = ["a-1", "a-2", "sub-1/b-1", "sub-1/b-2", "c-1", "u-1/x-1"]

    def _base_cfg(self):
        return _cfg(("body_a", ["a-1", "a-2"]), ("body_b", ["sub-1/b-1", "sub-1/b-2", "c-1"]))

    def test_leaf_only_selection_is_the_sorted_member_list(self):
        selections = _link_group_selections(self._base_cfg(), _scene(self.LEAVES), {})
        self.assertEqual(selections["body_a"]["selection"], ["a-1", "a-2"])
        self.assertEqual(selections["body_b"]["selection"], ["c-1", "sub-1/b-1", "sub-1/b-2"])
        self.assertEqual(selections["body_a"]["providers"], [])

    def test_top_most_provider_replaces_its_subtree(self):
        selections = _link_group_selections(self._base_cfg(), _scene(self.LEAVES), {"sub-1": {}})
        self.assertEqual(selections["body_b"]["selection"], ["sub-1", "c-1"])
        self.assertEqual(selections["body_b"]["providers"], ["sub-1"])
        self.assertEqual(selections["body_b"]["uncovered_leaves"], ["c-1"])

    def test_nested_providers_collapse_to_the_shallowest(self):
        effective = {"sub-1": {}, "sub-1/b-1": {}}
        selections = _link_group_selections(self._base_cfg(), _scene(self.LEAVES), effective)
        self.assertEqual(selections["body_b"]["selection"], ["sub-1", "c-1"])
        self.assertEqual(selections["body_b"]["providers"], ["sub-1"])

    def test_effective_rows_with_mode_are_providers_too(self):
        masses = {"sub-1/b-1": {"mode": "effective_instance"}}
        selections = _link_group_selections(self._base_cfg(), _scene(self.LEAVES, masses), {})
        self.assertEqual(selections["body_b"]["selection"], ["sub-1/b-1", "c-1", "sub-1/b-2"])

    def test_provider_spanning_bodies_fails_closed(self):
        cfg = _cfg(("body_a", ["sub-1/b-1"]), ("body_b", ["sub-1/b-2", "c-1"]))
        with self.assertRaises(BridgeError) as caught:
            _link_group_selections(cfg, _scene(self.LEAVES), {"sub-1": {}})
        self.assertEqual(caught.exception.code, "link_group_provider_spans_bodies")
        self.assertEqual(caught.exception.detail["bodies"], ["body_a", "body_b"])

    def test_provider_partially_assigned_fails_closed(self):
        cfg = _cfg(("body_b", ["sub-1/b-1", "c-1"]))
        with self.assertRaises(BridgeError) as caught:
            _link_group_selections(cfg, _scene(self.LEAVES), {"sub-1": {}})
        self.assertEqual(caught.exception.code, "link_group_provider_partial")

    def test_entirely_unassigned_subtree_is_not_a_provider(self):
        selections = _link_group_selections(self._base_cfg(), _scene(self.LEAVES), {"u-1": {}})
        self.assertEqual(selections["body_a"]["selection"], ["a-1", "a-2"])
        self.assertEqual(selections["body_b"]["selection"], ["c-1", "sub-1/b-1", "sub-1/b-2"])

    def test_effective_instance_without_an_occurrence_fails(self):
        with self.assertRaises(BridgeError) as caught:
            _link_group_selections(self._base_cfg(), _scene(self.LEAVES), {"ghost-1": {}})
        self.assertEqual(caught.exception.code, "link_group_provider_unknown")

    def test_unknown_and_duplicated_members_fail(self):
        cfg = _cfg(("body_a", ["a-1", "nope-1"]))
        with self.assertRaises(BridgeError) as caught:
            _link_group_selections(cfg, _scene(self.LEAVES), {})
        self.assertEqual(caught.exception.code, "link_group_member_unknown")
        cfg = _cfg(("body_a", ["a-1"]), ("body_b", ["a-1", "c-1"]))
        with self.assertRaises(BridgeError) as caught:
            _link_group_selections(cfg, _scene(self.LEAVES), {})
        self.assertEqual(caught.exception.code, "link_group_member_duplicate")

    def test_repeated_scene_occurrence_fails(self):
        with self.assertRaises(BridgeError) as caught:
            _link_group_selections(self._base_cfg(), _scene([*self.LEAVES, "a-1"]), {})
        self.assertEqual(caught.exception.code, "link_group_occurrence_duplicate")


class LinkGroupRecordTests(unittest.TestCase):
    SELECTION: ClassVar[list[str]] = ["a-1", "a-2", "sub-1/b-1", "sub-1/b-2"]

    def _selections(self):
        return {"body": {"selection": list(self.SELECTION), "providers": [], "uncovered_leaves": list(self.SELECTION)}}

    def _payload(self, *, selection=None, reads=None, assembly="C:/copy/robot.SLDASM"):
        return {
            "assembly": assembly,
            "configuration": "Default",
            "used_api": "IMassProperty2 (SelectedItems = component set)",
            "selection_rule": "top_most_providers+uncovered_leaves",
            "links": {
                "body": {
                    "selection": list(selection if selection is not None else self.SELECTION),
                    "reads": reads if reads is not None else [_read(), _read()],
                    "reproducibility": {"mass_delta_kg": 0.0, "com_delta_norm_m": 0.0, "tensor_delta_max_abs": 0.0},
                }
            },
        }

    def test_record_wraps_the_native_payload_with_the_schema(self):
        record = _link_group_record_from_payload(self._payload(), self._selections(), "C:/copy/robot.SLDASM")
        self.assertEqual(record["schema_version"], "solidworks-to-urdf.link-group-mass-properties/v1")
        self.assertEqual(record["assembly"], "C:/copy/robot.SLDASM")
        self.assertEqual(record["configuration"], "Default")
        self.assertEqual(record["used_api"], "IMassProperty2 (SelectedItems = component set)")
        self.assertEqual(record["selection_rule"], "top_most_providers+uncovered_leaves")
        self.assertEqual(sorted(record["links"]), ["body"])
        row = record["links"]["body"]
        self.assertEqual(row["selection"], self.SELECTION)
        self.assertEqual(len(row["reads"]), 2)
        self.assertEqual(
            row["reproducibility"],
            {"mass_delta_kg": 0.0, "com_delta_norm_m": 0.0, "tensor_delta_max_abs": 0.0},
        )

    def test_bad_schema_fields_and_link_coverage_fail_closed(self):
        bad = self._payload()
        bad["used_api"] = "other"
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(bad, self._selections(), "C:/copy/robot.SLDASM")
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")
        bad = self._payload()
        bad["selection_rule"] = "other"
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(bad, self._selections(), "C:/copy/robot.SLDASM")
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")
        bad = self._payload()
        bad["links"] = {}
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(bad, self._selections(), "C:/copy/robot.SLDASM")
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")

    def test_selection_echo_mismatch_fails_closed(self):
        bad = self._payload(selection=["a-1"])
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(bad, self._selections(), "C:/copy/robot.SLDASM")
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")

    def test_single_read_and_bad_read_fields_fail_closed(self):
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(
                self._payload(reads=[_read()]), self._selections(), "C:/copy/robot.SLDASM"
            )
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")
        bad_read = _read()
        bad_read["mass_kg"] = -1.0
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(
                self._payload(reads=[bad_read, _read()]), self._selections(), "C:/copy/robot.SLDASM"
            )
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")

    def test_other_document_read_fails_closed(self):
        payload = self._payload(assembly="C:/elsewhere/robot.SLDASM")
        with self.assertRaises(BridgeError) as caught:
            _link_group_record_from_payload(payload, self._selections(), "C:/copy/robot.SLDASM")
        self.assertEqual(caught.exception.code, "capture_source_mismatch")


if __name__ == "__main__":
    unittest.main()
