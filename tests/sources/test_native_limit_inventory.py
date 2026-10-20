"""Inventory proof that every active native mate was completely read."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.native_limits import (
    limit_entity_owners,
    native_limit_inventory_complete,
)


def active_mate(mate_type: str = "concentric") -> dict:
    return {
        "name": "native_mate",
        "type": mate_type,
        "suppressed": False,
        "error_code": 0,
        "limits": None,
        "native_limit": None,
        "is_advanced_mate": None,
        "is_advanced_mate_error": None,
        "is_advanced_mate_api": None,
    }


class NativeLimitInventoryTests(unittest.TestCase):
    def test_complete_inventory_requires_every_reading_on_each_active_mate(self) -> None:
        self.assertTrue(native_limit_inventory_complete([active_mate()]))
        for key in (
            "limits",
            "native_limit",
            "is_advanced_mate",
            "is_advanced_mate_error",
            "is_advanced_mate_api",
        ):
            with self.subTest(missing=key):
                mate = active_mate()
                mate.pop(key)
                self.assertFalse(native_limit_inventory_complete([mate]))

        suppressed = active_mate()
        suppressed["suppressed"] = True
        suppressed.pop("limits")
        self.assertTrue(native_limit_inventory_complete([suppressed]))

        for value in (None, "", " "):
            with self.subTest(type=value):
                mate = active_mate()
                mate["type"] = value
                self.assertFalse(native_limit_inventory_complete([mate]))

    def test_solve_state_requires_exact_integer_zero(self) -> None:
        for value in (False, 0.0, True, 1, None, "0"):
            with self.subTest(error_code=value):
                mate = active_mate()
                mate["error_code"] = value
                self.assertFalse(native_limit_inventory_complete([mate]))

    def test_position_limit_types_cannot_be_reclassified_as_ordinary_mates(self) -> None:
        for mate_type, api in (
            ("limitangle", "IAngleMateFeatureData.IsAdvancedMate"),
            ("limitdistance", "IDistanceMateFeatureData.IsAdvancedMate"),
        ):
            for case, update in (
                ("disabled_flag", {"is_advanced_mate": False, "is_advanced_mate_api": api}),
                ("wrong_api", {"is_advanced_mate": True, "is_advanced_mate_api": "IUnknown.IsAdvancedMate"}),
                ("missing_flag", {"is_advanced_mate": None, "is_advanced_mate_api": api}),
            ):
                with self.subTest(type=mate_type, case=case):
                    mate = active_mate(mate_type)
                    mate.update(update)
                    self.assertFalse(native_limit_inventory_complete([mate]))

    def test_standard_advanced_reading_must_be_explicit_and_type_bound(self) -> None:
        for mate_type, api in (
            ("angle", "IAngleMateFeatureData.IsAdvancedMate"),
            ("distance", "IDistanceMateFeatureData.IsAdvancedMate"),
        ):
            mate = active_mate(mate_type)
            mate.update({"is_advanced_mate": False, "is_advanced_mate_api": api})
            self.assertTrue(native_limit_inventory_complete([mate]))
            for case, update in (
                ("enabled", {"is_advanced_mate": True}),
                ("wrong_api", {"is_advanced_mate_api": "IUnknown.IsAdvancedMate"}),
                ("unread", {"is_advanced_mate": None}),
            ):
                with self.subTest(type=mate_type, case=case):
                    invalid = copy.deepcopy(mate)
                    invalid.update(update)
                    self.assertFalse(native_limit_inventory_complete([invalid]))

    def test_any_observed_limit_value_prevents_absence_proof(self) -> None:
        for key, value in (
            ("limits", {"lower": 0.0, "upper": 0.0, "unit": "rad"}),
            ("native_limit", {"type_index": 6}),
        ):
            with self.subTest(key=key):
                mate = active_mate()
                mate[key] = value
                self.assertFalse(native_limit_inventory_complete([mate]))

    def test_assembly_frame_entities_require_a_proven_owner(self) -> None:
        mate = {
            "entities": [
                {"component": "", "assembly_frame": True},
                {"component": "arm-1"},
            ]
        }
        owners = {"arm-1": "arm_link"}
        self.assertEqual(
            limit_entity_owners(mate, owners.get, assembly_owner="base_link"),
            ("base_link", "arm_link"),
        )
        self.assertIsNone(limit_entity_owners(mate, owners.get))
