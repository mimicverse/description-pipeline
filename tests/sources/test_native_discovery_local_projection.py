"""Linux discovery projection from preserved local native facts."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.discovery import (
    _project_native_record,
)
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.verification.native_discovery import (
    _Failure,
    project_native_verification_record,
)
from description_pipeline.verification.native_declarations import (
    verify_declarations,
)

from tests.sources import support


def _translated(x: float, y: float, z: float) -> list[float]:
    return [
        1.0,
        0.0,
        0.0,
        x,
        0.0,
        1.0,
        0.0,
        y,
        0.0,
        0.0,
        1.0,
        z,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def _rotated_z_90_with_translation() -> list[float]:
    return [
        0.0,
        -1.0,
        0.0,
        1.0,
        1.0,
        0.0,
        0.0,
        2.0,
        0.0,
        0.0,
        1.0,
        3.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def _record() -> dict:
    local_arm = _rotated_z_90_with_translation()
    return {
        "identity": {"main_assembly": "robot.SLDASM"},
        "components": [
            {
                "name2": "sub-1",
                "parent_occurrence": "",
                "document": "sub.SLDASM",
                "configuration": "Default",
                "suppressed": False,
                "transform_raw": support.solidworks_transform(
                    _translated(10.0, 0.0, 0.0)
                ),
            },
            {
                "name2": "sub-1/arm-1",
                "parent_occurrence": "sub-1",
                "document": "arm.SLDPRT",
                "configuration": "Sub",
                "suppressed": False,
                "transform_raw": support.solidworks_transform(local_arm),
            },
        ],
        "mates": [
            {
                "name": "nested_seat",
                "scope": "sub-1",
                "suppressed": False,
                "entities": [
                    {
                        "reference_name": "arm-1",
                        "reference_document": "arm.SLDPRT",
                        "reference_configuration": "Sub",
                        "assembly_frame": False,
                        "mate_entity_reference": {
                            "source": "mate-entity-params",
                            "reference_type": 0,
                            "reference_type2": 3,
                            "space": "mate-assembly",
                            "params": [2.0, 0.0, 4.0],
                            "error": None,
                        },
                    },
                    {
                        "reference_name": "sub",
                        "reference_document": "sub.SLDASM",
                        "reference_configuration": "Sub",
                        "assembly_frame": True,
                        "mate_entity_reference": {
                            "source": "mate-entity-params",
                            "reference_type": 3,
                            "reference_type2": 4,
                            "space": "mate-assembly",
                            "params": [
                                0.0,
                                0.0,
                                0.0,
                                0.0,
                                0.0,
                                1.0,
                                0.0,
                                0.0,
                            ],
                            "error": None,
                        },
                    },
                ],
            }
        ],
        "datums": [
            {
                "name": "CS_arm_link",
                "owner": "sub-1/arm-1",
                "configuration": "Sub",
                "array_raw": support.solidworks_transform(
                    _translated(0.0, 0.0, 0.5)
                ),
            }
        ],
        "properties": {"document": {}, "components": {}, "mates": {}},
    }


class NativeDiscoveryLocalProjectionTests(unittest.TestCase):
    def test_mate_reference_preserves_an_explicit_native_read_state(self) -> None:
        for field, value in (
            ("reference_type", None), ("params", None), ("error", None), ("error", False), ("error", ""),
        ):
            raw = _record()
            reference = raw["mates"][0]["entities"][0]["mate_entity_reference"]
            if value is None:
                reference.pop(field)
            else:
                reference[field] = value
            original = copy.deepcopy(raw)
            with self.subTest(field=field, value=value):
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)
                self.assertFalse(verify_declarations(raw)["passed"])
                self.assertEqual(raw, original)

    def test_mate_reference_requires_the_native_source_and_coordinate_space(self) -> None:
        for field in ("source", "space"):
            for value in (None, "wrong", 123):
                raw = _record()
                reference = raw["mates"][0]["entities"][0]["mate_entity_reference"]
                if value is None:
                    reference.pop(field)
                else:
                    reference[field] = value
                original = copy.deepcopy(raw)
                with self.subTest(field=field, value=value):
                    with self.assertRaises(CadError) as source_error:
                        _project_native_record(raw)
                    self.assertEqual(source_error.exception.code, "cad_mate_entity_reference_invalid")
                    with self.assertRaises(_Failure) as verification_error:
                        project_native_verification_record(raw)
                    self.assertEqual(verification_error.exception.code, "discovery.mate_entity_reference")
                    self.assertFalse(verify_declarations(raw)["passed"])
                    self.assertEqual(raw, original)

    def test_producer_and_verifier_recompute_the_same_world_facts(self) -> None:
        raw = _record()
        producer = _project_native_record(raw)
        verifier = project_native_verification_record(raw)

        self.assertNotIn("transform", raw["components"][0])
        self.assertNotIn("array", raw["datums"][0])
        for projected in (producer, verifier):
            arm = next(
                row
                for row in projected["components"]
                if row["name2"] == "sub-1/arm-1"
            )
            expected_world = [
                0.0,
                -1.0,
                0.0,
                11.0,
                1.0,
                0.0,
                0.0,
                2.0,
                0.0,
                0.0,
                1.0,
                3.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ]
            for actual, expected in zip(
                arm["transform"],
                expected_world,
                strict=True,
            ):
                self.assertAlmostEqual(actual, expected, places=12)
            reference = projected["mates"][0]["entities"][0][
                "mate_entity_reference"
            ]
            self.assertIsNone(reference["error"])
            self.assertEqual(reference["geometry"]["frame"], "component-local")
            for actual, expected in zip(
                reference["geometry"]["point"],
                [-2.0, -1.0, 1.0],
                strict=True,
            ):
                self.assertAlmostEqual(actual, expected, places=12)
            for actual, expected in zip(
                projected["datums"][0]["array"],
                [
                    0.0,
                    -1.0,
                    0.0,
                    11.0,
                    1.0,
                    0.0,
                    0.0,
                    2.0,
                    0.0,
                    0.0,
                    1.0,
                    3.5,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ],
                strict=True,
            ):
                self.assertAlmostEqual(actual, expected, places=12)

    def test_missing_parent_is_refused_by_both_independent_projections(self) -> None:
        raw = _record()
        raw["components"][1]["parent_occurrence"] = "missing"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_parent_cycle_is_refused_by_both_independent_projections(self) -> None:
        raw = _record()
        raw["components"][0]["parent_occurrence"] = "sub-1/arm-1"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_normalized_only_component_is_not_an_accepted_raw_form(self) -> None:
        raw = _record()
        component = raw["components"][0]
        component["transform"] = component.pop("transform_raw")
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_missing_and_null_transform_raw_are_not_empty_read_failures(self) -> None:
        for name, mutate in (
            ("missing", lambda component: component.pop("transform_raw")),
            ("null", lambda component: component.update(transform_raw=None)),
        ):
            with self.subTest(name=name):
                raw = _record()
                mutate(raw["components"][0])
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)

    def test_explicit_empty_transform_raw_remains_an_unreadable_frame(self) -> None:
        raw = _record()
        raw["components"][0]["transform_raw"] = []
        raw["mates"] = []
        raw["datums"] = []
        producer = _project_native_record(raw)
        verifier = project_native_verification_record(raw)
        for projected in (producer, verifier):
            component = next(
                item
                for item in projected["components"]
                if item["name2"] == "sub-1"
            )
            self.assertEqual(component["transform"], [])

    def test_parent_must_match_the_scoped_native_identity(self) -> None:
        raw = _record()
        raw["components"][1]["parent_occurrence"] = "other"
        raw["components"].append(
            {
                "name2": "other",
                "parent_occurrence": "",
                "document": "other.SLDASM",
                "suppressed": False,
                "transform_raw": support.solidworks_transform(
                    support.placement()
                ),
            }
        )
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_mate_scope_must_be_a_known_owner(self) -> None:
        raw = _record()
        raw["mates"][0]["scope"] = "missing"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_document_cycle_uses_normalized_path_semantics(self) -> None:
        raw = _record()
        raw["components"].append(
            {
                "name2": "robot-2",
                "parent_occurrence": "",
                "document": "./robot.SLDASM",
                "suppressed": False,
                "transform_raw": support.solidworks_transform(
                    support.placement()
                ),
            }
        )
        raw["mates"][0]["scope"] = ""
        raw["mates"][0]["entities"] = [
            {
                "reference_name": "robot-2",
                "reference_document": "./robot.SLDASM",
                "assembly_frame": False,
                "mate_entity_reference": {
                    "reference_type": None,
                    "reference_type2": None,
                    "space": "mate-assembly",
                    "params": None,
                    "error": None,
                },
            }
        ]
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_entity_params_typed_preconditions_fail_closed(self) -> None:
        cases = {
            "non-numeric": {
                "reference_type": 4,
                "params": ["bad"] * 7,
                "error": None,
            },
            "non-array": {
                "reference_type": 1,
                "params": "not-an-array",
                "error": None,
            },
            "boolean-element": {
                "reference_type": 1,
                "params": [0.0, 0.0, 0.0, 0.0, True, 0.0, 0.0, 0.0],
                "error": None,
            },
            "non-finite": {
                "reference_type": 3,
                "params": [0.0, 0.0, 0.0, 0.0, 0.0, float("nan")],
                "error": None,
            },
            "null-without-error": {
                "reference_type": 3,
                "params": None,
                "error": None,
            },
            "boolean-reference-type": {
                "reference_type": True,
                "params": [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
                "error": None,
            },
        }
        for name, reference in cases.items():
            with self.subTest(name=name):
                raw = _record()
                raw["mates"][0]["entities"][0][
                    "mate_entity_reference"
                ] = {"source": "mate-entity-params", "space": "mate-assembly", **reference}
                before = copy.deepcopy(raw)
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)
                report = verify_declarations(raw)
                self.assertFalse(report["passed"])
                self.assertEqual(
                    {item["code"] for item in report["errors"]},
                    {"discovery.mate_entity_reference"},
                )
                self.assertEqual(raw, before)

    def test_null_entity_params_with_native_error_remains_explicit(self) -> None:
        raw = _record()
        raw["mates"][0]["entities"][0]["mate_entity_reference"] = {
            "source": "mate-entity-params",
            "reference_type": None,
            "reference_type2": None,
            "space": "mate-assembly",
            "params": None,
            "error": "EntityParams is unavailable",
        }
        producer = _project_native_record(raw)
        verifier = project_native_verification_record(raw)
        for projected in (producer, verifier):
            reference = projected["mates"][0]["entities"][0][
                "mate_entity_reference"
            ]
            self.assertIsNone(reference["geometry"])
            self.assertEqual(reference["error"], "EntityParams is unavailable")

    def test_mate_document_mismatch_is_refused(self) -> None:
        raw = copy.deepcopy(_record())
        raw["mates"][0]["entities"][0]["reference_document"] = "other.SLDPRT"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_prelocalized_mate_geometry_is_not_trusted_as_raw(self) -> None:
        raw = copy.deepcopy(_record())
        raw["mates"][0]["entities"][0]["mate_entity_reference"][
            "geometry"
        ] = {"kind": "point", "point": [0.0, 0.0, 0.0]}
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_serialized_marker_cannot_authorize_normalized_only_raw(self) -> None:
        raw = _record()
        raw["_verification_local_projection"] = True
        component = raw["components"][0]
        component["transform"] = component.pop("transform_raw")
        original = copy.deepcopy(raw)

        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)
        self.assertEqual(raw, original)

    def test_only_the_private_typed_view_can_be_reentered(self) -> None:
        projected = project_native_verification_record(_record())
        repeated = project_native_verification_record(projected)
        self.assertEqual(repeated, projected)
        with self.assertRaises(_Failure):
            project_native_verification_record(dict(projected))


if __name__ == "__main__":
    unittest.main()
