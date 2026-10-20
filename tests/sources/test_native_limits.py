"""Pure validation of the raw native limit evidence."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.native_limits import validate_native_limit

from . import support


class NativeLimitValidationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
