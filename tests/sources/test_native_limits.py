"""Pure validation of the raw native limit evidence."""

from __future__ import annotations

import copy
import math
import unittest

from description_pipeline.sources.solidworks.native_limits import (
    ENGINEERING_DEFAULT_POLICY_ID,
    INSPECTION_PREVIEW_POLICY_ID,
    rotation_window_policy,
    limit_mate_scope,
    validate_limit_entity_topology,
    validate_native_limit,
)

from . import support


class NativeLimitValidationTests(unittest.TestCase):
    def test_rotation_windows_share_one_value_with_distinct_purpose_rules(self) -> None:
        author_limits = {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0}
        preview = rotation_window_policy(
            INSPECTION_PREVIEW_POLICY_ID,
            author_limits=author_limits,
            native_absence="complete_mate_inventory_no_enabled_limit",
        )
        engineering = rotation_window_policy(
            ENGINEERING_DEFAULT_POLICY_ID,
            author_limits=author_limits,
            native_absence="native_limit_observation_unreadable",
            native_errors=[{"mate": "limit", "reason": "unreadable"}],
        )
        self.assertEqual(preview["window_rad"], [-math.pi, math.pi])
        self.assertEqual(engineering["window_rad"], preview["window_rad"])
        self.assertNotEqual(preview["rule_id"], engineering["rule_id"])
        self.assertIs(engineering["engineering_position_range_qualified"], False)
        self.assertEqual(
            engineering["native_observation_errors"],
            [{"mate": "limit", "reason": "unreadable"}],
        )

    def test_complete_nonzero_nominal_observation(self) -> None:
        mate = support.native_limit_mate("base-1", "arm-1", lower=-0.01, upper=0.02)
        mate["limits"] = {"lower": 0.29, "upper": 0.32, "unit": "rad"}
        mate["native_limit"] = support.native_limit_observation(
            6, nominal=0.3, lower=0.29, upper=0.32
        )
        self.assertEqual(validate_native_limit(mate), (0.29, 0.32, "rad"))

    def test_zero_range_is_native_not_missing(self) -> None:
        mate = support.native_limit_mate("base-1", "arm-1", lower=0.2, upper=0.2)
        mate["native_limit"] = support.native_limit_observation(
            6, nominal=0.2, lower=0.2, upper=0.2
        )
        self.assertEqual(validate_native_limit(mate), (0.2, 0.2, "rad"))

    def test_missing_nominal_variation_or_entity_binding_is_refused(self) -> None:
        mate = support.native_limit_mate("base-1", "arm-1")
        cases = (
            lambda value: value["native_limit"].pop("nominal_dimension"),
            lambda value: value["native_limit"].pop("minimum_variation"),
            lambda value: value["native_limit"]["minimum_variation"].update({"value": -2.0}),
            lambda value: value["entities"][0].pop("reference_configuration"),
            lambda value: value["entities"][0].update(
                {"mate_entity_reference": {"error": "EntityParams is unavailable", "geometry": None}}
            ),
            lambda value: value["entities"][1].update({"entity_index": 0}),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                candidate = copy.deepcopy(mate)
                mutate(candidate)
                with self.assertRaises(ValueError):
                    validate_native_limit(candidate)

    def test_wrong_interface_and_legacy_limits_are_refused(self) -> None:
        mate = support.native_limit_mate("base-1", "arm-1")
        wrong = copy.deepcopy(mate)
        wrong["is_advanced_mate_api"] = "IDistanceMateFeatureData.IsAdvancedMate"
        with self.assertRaises(ValueError):
            validate_native_limit(wrong)
        legacy = copy.deepcopy(mate)
        legacy["native_limit"] = {"type_index": 6}
        with self.assertRaises(ValueError):
            validate_native_limit(legacy)

    def test_component_configuration_must_match_the_entity_binding(self) -> None:
        mate = support.native_limit_mate("base-1", "arm-1")
        components = {
            "base-1": {"configuration": "Default"},
            "arm-1": {"configuration": "Other"},
        }
        with self.assertRaisesRegex(ValueError, "configuration"):
            validate_native_limit(mate, components)

    def test_entities_must_span_parent_and_child_subtrees(self) -> None:
        mate = support.native_limit_mate("base-1", "arm-1")
        owners = {"base-1": "base", "arm-1": "arm"}
        self.assertEqual(
            validate_limit_entity_topology(mate, owners.get, {"arm"}, parent_owner="base"),
            ("base", "arm"),
        )
        same_side = copy.deepcopy(mate)
        same_side["entities"][1]["component"] = "base-1"
        with self.assertRaisesRegex(ValueError, "span"):
            validate_limit_entity_topology(same_side, owners.get, {"arm"}, parent_owner="base")

    def test_unrelated_mate_from_another_dimension_is_ignored(self) -> None:
        angle = support.native_limit_mate("base-1", "arm-1")
        distance = support.native_limit_mate("base-1", "base-2")
        owners = {"base-1": "base", "arm-1": "arm", "base-2": "base"}
        self.assertEqual(limit_mate_scope(angle, owners.get, {"arm"}, "hinge", parent_owner="base"), "crossing")
        self.assertEqual(limit_mate_scope(distance, owners.get, {"arm"}, "hinge", parent_owner="base"), "unrelated")
        unrelated_name = copy.deepcopy(angle)
        unrelated_name["name"] = "other__limit"
        unrelated_name["entities"][1]["component"] = "other-1"
        owners["other-1"] = "other"
        self.assertEqual(
            limit_mate_scope(unrelated_name, owners.get, {"arm"}, "hinge", parent_owner="base"),
            "unrelated",
        )
        unrelated_name["entities"][0]["component"] = "arm-1"
        self.assertEqual(
            limit_mate_scope(unrelated_name, owners.get, {"arm"}, "hinge", parent_owner="base"),
            "unrelated",
        )

    def test_assembly_frame_binds_only_with_an_explicit_root(self) -> None:
        mate = support.native_limit_mate("", "arm-1")
        mate["entities"][0]["assembly_frame"] = True
        owners = {"arm-1": "arm"}
        self.assertEqual(validate_native_limit(mate), (-1.0, 1.0, "rad"))
        self.assertEqual(
            limit_mate_scope(
                mate,
                owners.get,
                {"arm"},
                "hinge_joint",
                parent_owner="base",
                assembly_owner="base",
            ),
            "crossing",
        )
        self.assertEqual(
            limit_mate_scope(mate, owners.get, {"arm"}, "hinge_joint", parent_owner="base"),
            "claimed_unreadable",
        )
        self.assertEqual(
            validate_limit_entity_topology(
                mate,
                owners.get,
                {"arm"},
                parent_owner="base",
                assembly_owner="base",
            ),
            ("base", "arm"),
        )


if __name__ == "__main__":
    unittest.main()
