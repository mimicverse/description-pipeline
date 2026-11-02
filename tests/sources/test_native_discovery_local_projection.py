"""Linux discovery projection from preserved main-frame native facts."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.discovery import (
    _entity_geometry_view,
    _project_native_record,
)
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.selection import seal_closure_selection
from description_pipeline.verification.native_discovery import (
    _Failure,
    _geometry,
    project_native_verification_record,
)
from description_pipeline.verification.native_declarations import (
    verify_declarations,
)

from tests.sources import support
from tests.sources.test_verify_imported_sources import (
    CACHED_CAPTURE_DOCUMENT,
    CACHED_OCCURRENCE,
    cached_component,
    cached_slot,
)
from tests.sources.selection_support import (
    READER_SHA256,
    closure_evidence,
    seal_fixture_object_observation,
    seal_fixture_open_observation,
)


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
    world_arm = _rotated_z_90_with_translation()
    world_arm[3] += 10.0
    return {
        "transform_frame": "main_assembly",
        "identity": {"main_assembly": "robot.SLDASM"},
        "files": {
            "robot.SLDASM": "a" * 64,
            "sub.SLDASM": "b" * 64,
            "arm.SLDPRT": "c" * 64,
        },
        "saved_configuration": "Default",
        "components": [
            {
                "name2": "sub-1",
                "parent_occurrence": "",
                "document": "sub.SLDASM",
                "configuration": "Sub",
                "suppressed": False,
                "suppression_state": 2,
                "transform_raw": support.solidworks_transform(_translated(10.0, 0.0, 0.0)),
            },
            {
                "name2": "sub-1/arm-1",
                "parent_occurrence": "sub-1",
                "document": "arm.SLDPRT",
                "configuration": "Sub",
                "suppressed": False,
                "suppression_state": 2,
                "transform_raw": support.solidworks_transform(world_arm),
            },
        ],
        "mates": [
            {
                "name": "nested_seat",
                "scope": "sub-1",
                "configuration": "Sub",
                "suppressed": False,
                "entities": [
                    {
                        "reference_name": "arm-1",
                        "reference_document": "arm.SLDPRT",
                        "reference_configuration": "Sub",
                        "assembly_frame": False,
                        "mate_entity_reference": {
                            "source": "mate-entity-params",
                            "reference_type": 1,
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
                "array_raw": support.solidworks_transform(_translated(0.0, 0.0, 0.5)),
            }
        ],
        "properties": {"document": {}, "components": {}, "mates": {}},
    }


def _sealed_record() -> dict:
    return seal_fixture_object_observation(_record(), main_assembly="robot.SLDASM")


class NativeDiscoveryLocalProjectionTests(unittest.TestCase):
    @staticmethod
    def _cached_record(*, nested: bool = False) -> dict:
        raw = _sealed_record()
        anchor = "sub-1" if nested else None
        occurrence = f"sub-1/{CACHED_OCCURRENCE}" if nested else CACHED_OCCURRENCE
        component = cached_component(raw["files"]["robot.SLDASM"])
        component.update(
            name2=occurrence,
            instance_id=occurrence,
            parent_occurrence=anchor or "",
            transform_raw=support.solidworks_transform(_translated(0.0, 0.0, 0.0)),
            native_reference={
                "is_virtual": False,
                "native_owner": "robot.SLDASM",
                "defining": "sub.SLDASM" if nested else "robot.SLDASM",
                "defining_anchor": anchor,
            },
        )
        component["source_provenance"].update(occurrence=occurrence, defining_anchor=anchor)
        raw["components"].append(component)
        slot = cached_slot(
            raw["files"]["robot.SLDASM"], instance=occurrence, occurrence=occurrence,
            owner_anchor=anchor, defining_anchor=anchor,
        )
        closure = copy.deepcopy(raw["source_selection"]["closure"])
        if nested:
            parent = cached_component(raw["files"]["robot.SLDASM"])
            parent_document = parent["document"].replace("imported_part.step.SLDPRT", "sub.SLDASM")
            parent.update(
                name2=anchor, instance_id=anchor, parent_occurrence="", configuration="Sub",
                document=parent_document, transform_raw=raw["components"][0]["transform_raw"],
                native_reference={"is_virtual": False, "native_owner": "robot.SLDASM",
                                  "defining": "robot.SLDASM", "defining_anchor": None},
            )
            parent["source_provenance"].update(
                occurrence=anchor, configuration="Sub", native_document_path=parent_document,
                materialization_path=parent_document, session_document_path=parent_document,
            )
            raw["components"][0] = parent
            component["native_reference"]["defining"] = parent_document
            parent_slot = cached_slot(
                raw["files"]["robot.SLDASM"], instance=anchor, occurrence=anchor, configuration="Sub",
            )
            parent_slot["materialization"] = parent_slot["materialization"].replace(
                "imported_part.step.SLDPRT", "sub.SLDASM",
            )
            slot["document"] = parent_slot["materialization"]
            closure = closure_evidence(
                raw["files"], "robot.SLDASM",
                edges={"robot.SLDASM": ["arm.SLDPRT"], "arm.SLDPRT": []},
                imports=[slot, parent_slot],
            )
            closure["documents"]["robot.SLDASM"]["resolved_edges"][0].update(
                name="sub-1/arm-1", instance="sub-1/arm-1", configuration="Sub",
                owner_anchor=anchor, defining_session_document=parent_slot["materialization"],
            )
        else:
            closure["imports"] = [slot]
        raw["source_selection"] = seal_closure_selection(
            raw["files"], "robot.SLDASM", closure, reader_sha256=READER_SHA256,
        )
        seal_fixture_open_observation(raw)
        raw["session"] = {"native_api_pid": 1001}
        raw["mates"][0].update(scope="", configuration="Default")
        raw["mates"][0]["entities"] = [{
            "reference_name": occurrence,
            "reference_document": CACHED_CAPTURE_DOCUMENT,
            "reference_configuration": "Default",
            "assembly_frame": False,
            "mate_entity_reference": {
                "source": "mate-entity-params", "reference_type": 0,
                "space": "mate-assembly", "params": [1.0, 2.0, 3.0], "error": None,
            },
        }]
        return raw

    def test_file_inventory_without_a_sealed_observation_cannot_bind_mate_endpoints(self):
        raw = _record()
        original = copy.deepcopy(raw)
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)
        self.assertEqual(raw, original)

    def test_cached_mate_endpoint_binds_separate_owned_sessions_and_nested_defining_document(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                raw = self._cached_record(nested=nested)
                original = copy.deepcopy(raw)
                producer = _project_native_record(raw)
                verifier = project_native_verification_record(raw)
                self.assertEqual(producer["mates"][0]["entities"], verifier["mates"][0]["entities"])
                self.assertEqual(raw, original)

    def test_cached_mate_endpoint_refuses_unsealed_foreign_and_contradictory_bindings(self):
        mutations = {
            "session_pid": lambda r: r["session"].update(native_api_pid=1002),
            "boolean_pid": lambda r: r["components"][-1]["source_provenance"].update(native_api_pid=True),
            "owner_bytes": lambda r: r["components"][-1]["source_provenance"].update(owner_sha256="f" * 64),
            "defining_document": lambda r: r["components"][-1]["native_reference"].update(defining="foreign.SLDASM"),
            "virtual_conflict": lambda r: r["components"][-1]["native_reference"].update(is_virtual=True),
            "current_path": lambda r: r["components"][-1]["source_provenance"].update(
                session_document_path=r"C:\foreign\part.SLDPRT",
            ),
            "selection_root": lambda r: r["source_selection"]["closure"]["imports"][0].update(
                session_temp_root=r"C:\Temp\swx1001",
            ),
            "selection_digest": lambda r: r["source_selection"]["closure"]["imports"][0].update(
                session_source_sha256="0" * 64,
            ),
            "current_author_presence": lambda r: r["components"][-1]["source_provenance"].update(
                author_imported_exists=True,
            ),
            "current_author_conflict": lambda r: r["components"][-1]["source_provenance"].update(
                author_imported_path=r"D:\missing.step", author_imported_error="unreadable",
                author_imported_exists=False,
            ),
            "slot_author_presence": lambda r: r["source_selection"]["closure"]["imports"][0].update(
                author_imported_exists=True,
            ),
            "slot_author_conflict": lambda r: r["source_selection"]["closure"]["imports"][0].update(
                author_imported_path=r"D:\missing.step", author_imported_error="unreadable",
                author_imported_exists=False,
            ),
            "slot_suppression": lambda r: r["source_selection"]["closure"]["imports"][0].update(
                suppression_state=0,
            ),
            "duplicate_slot": lambda r: r["source_selection"]["closure"]["imports"].append(
                copy.deepcopy(r["source_selection"]["closure"]["imports"][0]),
            ),
            "malformed_carrier": lambda r: r.update(source_selection=[]),
            "malformed_closure": lambda r: r["source_selection"].update(closure=[]),
            "malformed_imports": lambda r: r["source_selection"]["closure"].update(imports={}),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                raw = self._cached_record(nested=True)
                mutate(raw)
                original = copy.deepcopy(raw)
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)
                self.assertEqual(raw, original)

    def test_mate_reference_preserves_an_explicit_native_read_state(self) -> None:
        for field, value in (
            ("reference_type", None), ("params", None), ("error", None), ("error", False), ("error", ""),
        ):
            raw = _sealed_record()
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
                raw = _sealed_record()
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
        raw = _sealed_record()
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

    def test_rotated_parent_world_contract_has_independent_numeric_oracles(self):
        parent = [0.0, -1.0, 0.0, 10.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        child = [-1.0, 0.0, 0.0, 11.0, 0.0, -1.0, 0.0, 2.0, 0.0, 0.0, 1.0, 3.0, 0.0, 0.0, 0.0, 1.0]
        datum = [1.0, 0.0, 0.0, 0.5, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        raw = _record()
        raw["saved_configuration"] = "Robot"
        raw["components"][0]["transform_raw"] = support.solidworks_transform(parent)
        raw["components"][1].update(configuration="Child", transform_raw=support.solidworks_transform(child))
        raw["mates"] = [
            {
                "name": "point",
                "scope": "sub-1",
                "configuration": "Sub",
                "suppressed": False,
                "entities": [
                    {
                        "reference_name": "arm-1",
                        "reference_document": "arm.SLDPRT",
                        "reference_configuration": "Child",
                        "assembly_frame": False,
                        "mate_entity_reference": {
                            "source": "mate-entity-params",
                            "reference_type": 1,
                            "reference_type2": 3,
                            "space": "mate-assembly",
                            "params": [2.0, 0.0, 0.0],
                            "error": None,
                        },
                    }
                ],
            }
        ]
        raw["datums"] = [
            {
                "name": "CS",
                "owner": "sub-1/arm-1",
                "array_raw": support.solidworks_transform(datum),
                "configuration": "Child",
                "references": [],
            }
        ]
        raw = seal_fixture_object_observation(raw, main_assembly="robot.SLDASM")
        original = copy.deepcopy(raw)
        for project in (_project_native_record, project_native_verification_record):
            with self.subTest(project=project.__name__):
                result = project(raw)
                self.assertEqual(result["components"][1]["transform"], child)
                self.assertEqual(
                    result["mates"][0]["entities"][0]["mate_entity_reference"]["geometry"]["point"], [1.0, 0.0, -3.0]
                )
                self.assertEqual([result["datums"][0]["array"][i] for i in (3, 7, 11)], [10.5, 2.0, 3.0])
        self.assertEqual(raw, original)

    def test_missing_or_foreign_world_frame_is_rejected_by_both_projections(self):
        for raw in (None, [], "record", True, 1):
            with self.subTest(raw=raw), self.assertRaises(CadError) as producer:
                _project_native_record(raw)
            self.assertEqual(producer.exception.code, "cad_native_record_invalid")
            with self.subTest(raw=raw), self.assertRaises(_Failure) as verifier:
                project_native_verification_record(raw)
            self.assertEqual(verifier.exception.code, "discovery.raw")
        for frame in (None, "child_document"):
            raw = _sealed_record()
            if frame is None:
                raw.pop("transform_frame")
            else:
                raw["transform_frame"] = frame
            for project in (_project_native_record, project_native_verification_record):
                with self.subTest(frame=frame, project=project.__name__), self.assertRaises((CadError, _Failure)):
                    project(raw)

    def test_missing_parent_is_refused_by_both_independent_projections(self) -> None:
        raw = _sealed_record()
        raw["components"][1]["parent_occurrence"] = "missing"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_parent_cycle_is_refused_by_both_independent_projections(self) -> None:
        raw = _sealed_record()
        raw["components"][0]["parent_occurrence"] = "sub-1/arm-1"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_normalized_only_component_is_not_an_accepted_raw_form(self) -> None:
        raw = _sealed_record()
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
                raw = _sealed_record()
                mutate(raw["components"][0])
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)

    def test_explicit_empty_transform_raw_remains_an_unreadable_frame(self) -> None:
        raw = _sealed_record()
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
        raw = _sealed_record()
        raw["components"][1]["parent_occurrence"] = "other"
        raw["components"].append(
            {
                "name2": "other",
                "parent_occurrence": "",
                "document": "other.SLDASM",
                "suppressed": False,
                "suppression_state": 2,
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
        raw = _sealed_record()
        raw["mates"][0]["scope"] = "missing"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_document_cycle_uses_normalized_path_semantics(self) -> None:
        raw = _sealed_record()
        raw["components"].append(
            {
                "name2": "robot-2",
                "parent_occurrence": "",
                "document": "./robot.SLDASM",
                "suppressed": False,
                "suppression_state": 2,
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
                "reference_type": 2,
                "params": "not-an-array",
                "error": None,
            },
            "boolean-element": {
                "reference_type": 2,
                "params": [0.0, 0.0, 0.0, 0.0, True, 0.0, 0.0, 0.0],
                "error": None,
            },
            "non-finite": {
                "reference_type": 3,
                "params": [0.0, 0.0, 0.0, 0.0, 0.0, float("nan")],
                "error": None,
            },
            "non-array-with-native-error": {
                "reference_type": None,
                "params": "not-an-array",
                "error": "RuntimeError: ReferenceType getter failed",
            },
            "non-numeric-with-native-error": {
                "reference_type": None,
                "params": ["bad"] * 7,
                "error": "RuntimeError: ReferenceType getter failed",
            },
            "boolean-element-with-native-error": {
                "reference_type": None,
                "params": [0.0, 0.0, 0.0, 0.0, True, 0.0, 0.0, 0.0],
                "error": "RuntimeError: ReferenceType getter failed",
            },
            "non-finite-with-native-error": {
                "reference_type": None,
                "params": [0.0, 0.0, 0.0, 0.0, 0.0, float("inf")],
                "error": "RuntimeError: ReferenceType getter failed",
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
                raw = _sealed_record()
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
        raw = _sealed_record()
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

    def test_reference_type_error_preserves_params_and_primary_evidence(self) -> None:
        primary_cases = {
            "plane": {
                "plane": {
                    "point": [0.0, 0.0, 0.0],
                    "normal": [0.0, 0.0, 1.0],
                }
            },
            "cylinder": {
                "cylinder": {
                    "point": [0.0, 0.0, 0.0],
                    "direction": [0.0, 0.0, 1.0],
                    "radius": 0.1,
                }
            },
            "point": {"point": [0.0, 0.0, 0.0]},
        }
        for name, primary in primary_cases.items():
            with self.subTest(name=name):
                raw = _sealed_record()
                entity = raw["mates"][0]["entities"][0]
                entity.update(primary)
                reference = entity["mate_entity_reference"]
                reference.update(
                    reference_type=None,
                    params=[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.1, 0.0],
                    error="RuntimeError: ReferenceType getter failed",
                )
                original = copy.deepcopy(raw)

                projected_rows = (
                    _project_native_record(raw),
                    project_native_verification_record(raw),
                )
                self.assertEqual(raw, original)
                for projected in projected_rows:
                    projected_entity = projected["mates"][0]["entities"][0]
                    projected_reference = projected_entity[
                        "mate_entity_reference"
                    ]
                    self.assertEqual(
                        projected_reference["params"],
                        [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.1, 0.0],
                    )
                    self.assertEqual(
                        projected_reference["error"],
                        "RuntimeError: ReferenceType getter failed",
                    )
                    self.assertIsNone(projected_reference["geometry"])

                source_findings: list[dict] = []
                source_entity = projected_rows[0]["mates"][0]["entities"][0]
                verify_entity = projected_rows[1]["mates"][0]["entities"][0]
                self.assertIsNotNone(
                    _entity_geometry_view(
                        source_entity,
                        source_findings,
                        "mate:nested_seat",
                    )
                )
                self.assertEqual(source_findings, [])
                point, direction = _geometry(
                    verify_entity,
                    {
                        "sub-1": [
                            [1.0, 0.0, 0.0, 0.0],
                            [0.0, 1.0, 0.0, 0.0],
                            [0.0, 0.0, 1.0, 0.0],
                            [0.0, 0.0, 0.0, 1.0],
                        ],
                        "sub-1/arm-1": [
                            [1.0, 0.0, 0.0, 0.0],
                            [0.0, 1.0, 0.0, 0.0],
                            [0.0, 0.0, 1.0, 0.0],
                            [0.0, 0.0, 0.0, 1.0],
                        ],
                    },
                )
                self.assertIsNotNone(point)
                if name != "point":
                    self.assertIsNotNone(direction)

    def test_reference_type_error_without_primary_is_still_rejected_downstream(self) -> None:
        raw = _sealed_record()
        raw["mates"][0]["entities"][0]["mate_entity_reference"].update(
            reference_type=None,
            params=[0.0, 0.0, 0.0],
            error="RuntimeError: ReferenceType getter failed",
        )
        source = _project_native_record(raw)
        verifier = project_native_verification_record(raw)
        source_entity = source["mates"][0]["entities"][0]
        verify_entity = verifier["mates"][0]["entities"][0]
        findings: list[dict] = []
        self.assertIsNone(
            _entity_geometry_view(
                source_entity,
                findings,
                "mate:nested_seat",
            )
        )
        self.assertEqual(
            [finding["code"] for finding in findings],
            ["discovery.mate_entities_unsupported"],
        )
        self.assertEqual(
            _geometry(
                verify_entity,
                {
                    "sub-1": [
                        [1.0, 0.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ],
                    "sub-1/arm-1": [
                        [1.0, 0.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0, 0.0],
                        [0.0, 0.0, 1.0, 0.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ],
                },
            ),
            (None, None),
        )

    def test_mate_document_mismatch_is_refused(self) -> None:
        raw = copy.deepcopy(_sealed_record())
        raw["mates"][0]["entities"][0]["reference_document"] = "other.SLDPRT"
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_component_document_must_belong_to_the_sealed_file_inventory(
        self,
    ) -> None:
        raw = copy.deepcopy(_sealed_record())
        raw["components"][0]["document"] = "forged.SLDPRT"
        raw["mates"][0]["entities"][0]["reference_document"] = (
            "forged.SLDPRT"
        )
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_mate_reference_requires_an_explicit_document_identity(self) -> None:
        for label, mutate in (
            (
                "missing",
                lambda entity: entity.pop(
                    "reference_document",
                    None,
                ),
            ),
            (
                "null",
                lambda entity: entity.__setitem__(
                    "reference_document",
                    None,
                ),
            ),
            (
                "empty",
                lambda entity: entity.__setitem__(
                    "reference_document",
                    "",
                ),
            ),
        ):
            with self.subTest(label=label):
                raw = copy.deepcopy(_sealed_record())
                mutate(raw["mates"][0]["entities"][0])
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)

    def test_component_mate_reference_requires_its_exact_configuration(self) -> None:
        for label, mutate in (
            (
                "missing",
                lambda entity: entity.pop(
                    "reference_configuration",
                    None,
                ),
            ),
            (
                "null",
                lambda entity: entity.__setitem__(
                    "reference_configuration",
                    None,
                ),
            ),
            (
                "empty",
                lambda entity: entity.__setitem__(
                    "reference_configuration",
                    "",
                ),
            ),
            (
                "wrong",
                lambda entity: entity.__setitem__(
                    "reference_configuration",
                    "Other",
                ),
            ),
        ):
            with self.subTest(label=label):
                raw = copy.deepcopy(_sealed_record())
                mutate(raw["mates"][0]["entities"][0])
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)

    def test_mate_configuration_must_match_its_owner_assembly(self) -> None:
        for label, mutate in (
            (
                "missing",
                lambda mate: mate.pop("configuration", None),
            ),
            (
                "null",
                lambda mate: mate.__setitem__("configuration", None),
            ),
            (
                "empty",
                lambda mate: mate.__setitem__("configuration", ""),
            ),
            (
                "wrong",
                lambda mate: mate.__setitem__("configuration", "Other"),
            ),
        ):
            with self.subTest(label=label):
                raw = copy.deepcopy(_sealed_record())
                mutate(raw["mates"][0])
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)

    def test_assembly_frame_document_must_equal_its_owner_assembly(self) -> None:
        def frame_record() -> dict:
            raw = copy.deepcopy(_sealed_record())
            mate = raw["mates"][0]
            mate["scope"] = ""
            mate["configuration"] = "Default"
            entity = mate["entities"][1]
            entity.update(
                reference_name="assembly_root",
                reference_document="robot.SLDASM",
                assembly_frame=True,
            )
            mate["entities"] = [entity]
            return raw

        baseline = frame_record()
        self.assertIsNotNone(_project_native_record(baseline))
        self.assertIsNotNone(
            project_native_verification_record(baseline)
        )
        for value in (None, "", "NotTheOwnerConfig"):
            raw = frame_record()
            raw["mates"][0]["entities"][0][
                "reference_configuration"
            ] = value
            with self.subTest(reference_configuration=value):
                self.assertIsNotNone(_project_native_record(raw))
                self.assertIsNotNone(
                    project_native_verification_record(raw)
                )
        for label, mutate in (
            (
                "missing",
                lambda entity: entity.pop(
                    "reference_configuration",
                    None,
                ),
            ),
            (
                "wrong-type",
                lambda entity: entity.__setitem__(
                    "reference_configuration",
                    1,
                ),
            ),
        ):
            raw = frame_record()
            mutate(raw["mates"][0]["entities"][0])
            with self.subTest(configuration_state=label):
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)
        for label, mutate in (
            (
                "missing",
                lambda entity: entity.pop(
                    "reference_document",
                    None,
                ),
            ),
            (
                "null",
                lambda entity: entity.__setitem__(
                    "reference_document",
                    None,
                ),
            ),
            (
                "wrong",
                lambda entity: entity.__setitem__(
                    "reference_document",
                    "other.SLDASM",
                ),
            ),
        ):
            with self.subTest(label=label):
                raw = frame_record()
                mutate(raw["mates"][0]["entities"][0])
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)

    def test_only_a_sealed_embedded_virtual_outside_document_is_accepted(self) -> None:
        owner_sha256 = "a" * 64
        virtual_document = (
            r"C:\Users\operator\AppData\Local\Temp\swx21040"
            r"\VC~~\waist\virtual_part.SLDPRT"
        )
        provenance = {
            "state": "embedded_virtual_component",
            "kind": "native_embedded_virtual_component",
            "owner": "robot.SLDASM",
            "owner_sha256": owner_sha256,
            "occurrence": "virtual-1",
            "defining_anchor": None,
            "native_document_path": virtual_document,
            "session_document_path": virtual_document,
            "materialization_path": virtual_document,
            "session_temp_root": (
                r"C:\Users\operator\AppData\Local\Temp\swx21040"
            ),
            "native_api_pid": 21040,
            "author_imported_path": None,
            "author_imported_error": None,
            "is_virtual": True,
            "used_api": (
                "IComponent2.GetPathName/"
                "IComponent2.GetImportedPath/"
                "IComponent2.IsVirtual"
            ),
            "document": None,
            "error": None,
        }

        def virtual_record() -> dict:
            raw = copy.deepcopy(_sealed_record())
            raw["components"].append(
                {
                    "name2": "virtual-1",
                    "parent_occurrence": "",
                    "document": virtual_document,
                    "configuration": "Default",
                    "suppressed": False,
                    "suppression_state": 2,
                    "lightweight": False,
                    "transform_raw": support.solidworks_transform(
                        _translated(0.0, 0.0, 0.0)
                    ),
                    "source_provenance": copy.deepcopy(provenance),
                }
            )
            closure = copy.deepcopy(raw["source_selection"]["closure"])
            closure["virtual_components"] = [{
                **copy.deepcopy(provenance),
                "document": "robot.SLDASM", "binding": "owner_embedded_geometry",
                "owner_anchor": None, "instance": "virtual-1", "configuration": "Default",
                "suppression_state": 2,
            }]
            raw["source_selection"] = seal_closure_selection(
                raw["files"], "robot.SLDASM", closure, reader_sha256=READER_SHA256,
            )
            seal_fixture_open_observation(raw)
            raw["session"] = {"native_api_pid": 21040}
            mate = raw["mates"][0]
            mate["scope"] = ""
            mate["configuration"] = "Default"
            mate["entities"] = [
                {
                    "reference_name": "virtual-1",
                    "reference_document": virtual_document,
                    "reference_configuration": "Default",
                    "assembly_frame": False,
                    "mate_entity_reference": {
                        "source": "mate-entity-params",
                        "reference_type": 1,
                        "reference_type2": 3,
                        "space": "mate-assembly",
                        "params": [0.0, 0.0, 0.0],
                        "error": None,
                    },
                }
            ]
            return raw

        qualified = virtual_record()
        self.assertIsNotNone(_project_native_record(qualified))
        self.assertIsNotNone(
            project_native_verification_record(qualified)
        )

        invalid_provenance = virtual_record()
        invalid_provenance["components"][-1]["source_provenance"][
            "native_api_pid"
        ] = 99999
        with self.assertRaises(CadError):
            _project_native_record(invalid_provenance)
        with self.assertRaises(_Failure):
            project_native_verification_record(invalid_provenance)

        wrong_document = virtual_record()
        wrong_document["mates"][0]["entities"][0][
            "reference_document"
        ] = r"C:\elsewhere\virtual_part.SLDPRT"
        with self.assertRaises(CadError):
            _project_native_record(wrong_document)
        with self.assertRaises(_Failure):
            project_native_verification_record(wrong_document)

    def test_prelocalized_mate_geometry_is_not_trusted_as_raw(self) -> None:
        raw = copy.deepcopy(_sealed_record())
        raw["mates"][0]["entities"][0]["mate_entity_reference"][
            "geometry"
        ] = {"kind": "point", "point": [0.0, 0.0, 0.0]}
        with self.assertRaises(CadError):
            _project_native_record(raw)
        with self.assertRaises(_Failure):
            project_native_verification_record(raw)

    def test_serialized_marker_cannot_authorize_normalized_only_raw(self) -> None:
        raw = _sealed_record()
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
        projected = project_native_verification_record(_sealed_record())
        repeated = project_native_verification_record(projected)
        self.assertEqual(repeated, projected)
        with self.assertRaises(_Failure):
            project_native_verification_record(dict(projected))

    def test_malformed_mate_and_datum_containers_fail_precisely(self) -> None:
        for label, mutate in (
            (
                "mates",
                lambda raw: raw.__setitem__("mates", "bad"),
            ),
            (
                "entities",
                lambda raw: raw["mates"][0].__setitem__(
                    "entities",
                    1,
                ),
            ),
            (
                "datums",
                lambda raw: raw.__setitem__("datums", 1),
            ),
        ):
            with self.subTest(label=label):
                raw = copy.deepcopy(_sealed_record())
                mutate(raw)
                with self.assertRaises(CadError):
                    _project_native_record(raw)
                with self.assertRaises(_Failure):
                    project_native_verification_record(raw)


if __name__ == "__main__":
    unittest.main()
