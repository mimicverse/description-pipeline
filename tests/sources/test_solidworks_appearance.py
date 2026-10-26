"""Appearance raw remains evidence until every delivered face can be represented."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unittest
from pathlib import Path

from description_pipeline.sources.solidworks.appearance import (
    AppearanceError,
    _rgba_from_colorref,
    _validate_index_conversion,
    appearance_groups,
    bind_capture_appearance,
    resolve_deliverable_appearance,
)
from description_pipeline.geometry.native_tessellation import native_stl_subset

from . import support

COMPONENT = "arm-1"
DOCUMENT = r"C:\capture\arm.SLDPRT"
CONFIGURATION = "Painted"
PART_FACE_OVERRIDE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "native-appearance-part-face-override.json"
)


def _layer(
    *,
    owner_configuration: str,
    values=None,
    has_values: bool = False,
    texture_state: str = "absent",
    texture_name=None,
    read_errors=None,
):
    return {
        "used_api": f"appearance/{owner_configuration}",
        "material_call": {
            "configuration_semantics": "requested_configuration",
            "configuration_option": 3,
            "configuration_names": [owner_configuration],
        },
        "presence_call": {
            "arguments": [],
            "configuration_semantics": "native_owner_context",
        },
        "material_property_values": values,
        "has_material_property_values": has_values,
        "texture": {
            "configuration": owner_configuration,
            "state": texture_state,
            "material_name": texture_name,
        },
        "read_errors": dict(read_errors or {}),
    }


def _body_layer(
    *,
    active_configuration: str,
    values=None,
    has_values: bool = False,
    texture_state: str = "absent",
    texture_name=None,
    read_errors=None,
):
    layer = _layer(
        owner_configuration=active_configuration,
        values=values,
        has_values=has_values,
        texture_state=texture_state,
        texture_name=texture_name,
        read_errors=read_errors,
    )
    layer["material_call"] = {
        "configuration_semantics": "active_configuration",
        "active_configuration": active_configuration,
    }
    return layer


def _raw(
    *,
    document=None,
    occurrence=None,
    body=None,
    active_configuration: str = CONFIGURATION,
    active_configuration_error=None,
):
    return {
        "component": COMPONENT,
        "source_document_path": DOCUMENT,
        "referenced_configuration": CONFIGURATION,
        "native_active_configuration": active_configuration,
        "native_active_configuration_error": active_configuration_error,
        "document": document
        or _layer(owner_configuration=CONFIGURATION),
        "occurrence": occurrence
        or _layer(owner_configuration=CONFIGURATION),
        "bodies": [
            {
                "component": COMPONENT,
                "enumeration_api": "IComponent2.GetBodies3",
                "body_type": 0,
                "body_index": 0,
                "body_name": "arm",
                "appearance": body
                or _body_layer(active_configuration=active_configuration),
            }
        ],
    }


def _empty_state_owner(
    *,
    scope: str,
    document_path: str,
    configuration: str,
    display_state: str,
    display_states=None,
    display_states_state: str = "not_applicable",
):
    return {
        "owner_document_path": document_path,
        "owner_scope": scope,
        "api": "IModelDocExtension.GetRenderMaterials2",
        "display_state_option": 3,
        "configuration": configuration,
        "display_state": display_state,
        "display_states": display_states,
        "display_states_state": display_states_state,
        "native_render_material_count_api": (
            "IModelDocExtension.GetRenderMaterialsCount2"
        ),
        "native_render_material_count": 0,
        "native_render_material_count_error": None,
        "call_state": "empty",
        "materials": [],
        "mapping_errors": [],
        "native_reference_index": [],
        "read_errors": {},
    }


def _binding_context(
    *,
    owner_scope: str,
    owner_document_path: str,
    api: str = "IModelDocExtension.GetPersistReference3",
):
    return {
        "owner_scope": owner_scope,
        "owner_document_path": owner_document_path,
        "component": COMPONENT,
        "api": api,
    }


def _state_entity_record(
    *,
    kind: str = "entity",
    reference=(1, 2, 3),
    owner_scope: str = "component_document",
    owner_document_path: str = DOCUMENT,
    native_interface_kinds=None,
):
    interface_kinds = (
        list(native_interface_kinds)
        if native_interface_kinds is not None
        else ["IFace2", "IEntity"]
    )
    return {
        "entity_index": 0,
        "native_object_type": "_CDispatch",
        "native_interface_kinds": interface_kinds,
        "persistent_reference": list(reference),
        "binding_context": _binding_context(
            owner_scope=owner_scope,
            owner_document_path=owner_document_path,
        ),
        "native_identity": {
            "kind": "reference",
            "state": "observed",
            "proof_api": (
                "IModelDocExtension.GetObjectByPersistReference3"
            ),
            "status": 0,
            "owner_document_path": owner_document_path,
            "interface_kinds": interface_kinds,
            "read_errors": {},
        },
        "read_errors": {},
    }


def _reference_row(
    *,
    reference,
    target,
    owner_scope: str = "component_document",
    owner_document_path: str = DOCUMENT,
):
    target_kind = target.get("kind")
    if target_kind == "body":
        target = {
            "kind": "body",
            "component": target.get("component", COMPONENT),
            "faces": [
                {
                    "body_type": target.get("body_type"),
                    "body_index": target.get("body_index"),
                    "face_index": target.get("face_index", 0),
                }
            ],
            "occurrence_bodies": [
                [
                    target.get("body_type"),
                    target.get("body_index"),
                ]
            ],
            "persistent_reference_sha256": hashlib.sha256(
                bytes(item & 0xFF for item in reference)
            ).hexdigest(),
        }
    is_assembly_occurrence = target_kind == "occurrence"
    is_body = target_kind == "body"
    is_feature = target_kind == "feature"
    return {
        "persistent_reference": list(reference),
        "native_interface_kind": (
            "IEntity"
            if target.get("kind") == "face"
            else "IBody2"
            if target.get("kind") == "body"
            else None
        ),
        "read_errors": {},
        "conversion": (
            None
            if is_assembly_occurrence
            else {
                "component": target.get("component", COMPONENT),
                "input_context": {
                    "owner_scope": "assembly_occurrence",
                    "object_kind": "assembly_occurrence",
                    "source_api": (
                        "IComponent2.GetBodies3/IBody2.GetFaces"
                    ),
                    "traversal_api": (
                        "IConfiguration.GetRootComponent3/"
                        "IComponent2.GetChildren"
                    ),
                },
                "receiver": {
                    "owner_scope": "component_document",
                    "owner_document_path": owner_document_path,
                    "interface": "IModelDocExtension",
                    "object_source": "captured_component_source_document",
                },
                "api": (
                    "IFace2.GetBody+"
                    "IModelDocExtension.GetPersistReference3"
                    if is_body
                    else "IFace2.GetFeature+"
                    "IModelDocExtension.GetPersistReference3"
                    if is_feature
                    else "IModelDocExtension.GetCorrespondingEntity2"
                ),
                "output_interface_kind": (
                    "IBody2"
                    if is_body
                    else "IFeature"
                    if is_feature
                    else "IFace2"
                ),
                "output_domain": {
                    "proof_api": (
                        "IModelDocExtension.GetObjectByPersistReference3"
                    ),
                    "status": 0,
                    "interface_kind": (
                        "IBody2"
                        if is_body
                        else "IFeature"
                        if is_feature
                        else "IFace2"
                    ),
                    "expected_owner_document_path": owner_document_path,
                },
                "state": "observed",
                "read_errors": {},
            }
        ),
        "binding_context": _binding_context(
            owner_scope=owner_scope,
            owner_document_path=owner_document_path,
        ),
        "target": target,
    }


def _resolved_raw(values):
    face_rows = []
    for index, face_values in enumerate(values):
        face_rows.append(
            {
                "face_index": index,
                "appearance": _layer(
                    owner_configuration=CONFIGURATION,
                    values=face_values,
                    has_values=True,
                ),
            }
        )
    raw = {
        **_raw(),
        "native_referenced_display_state": "Display State 1",
        "native_referenced_display_state_error": None,
        "state_render_materials": [
            _empty_state_owner(
                scope="component_document",
                document_path=DOCUMENT,
                configuration=CONFIGURATION,
                display_state="Display State 1",
            )
        ],
        "bodies": [
            {
                "component": COMPONENT,
                "enumeration_api": "IComponent2.GetBodies3",
                "body_type": 0,
                "body_index": 0,
                "body_name": "arm",
                "appearance": _body_layer(
                    active_configuration=CONFIGURATION
                ),
                "faces": face_rows,
            }
        ],
    }
    for body in raw["bodies"]:
        for face in body["faces"]:
            face_key = (
                body["body_type"],
                body["body_index"],
                face["face_index"],
            )
            face_row = _reference_row(
                reference=(1, 2, face_key[2] + 3),
                target={
                    "kind": "face",
                    "component": COMPONENT,
                    "body_type": face_key[0],
                    "body_index": face_key[1],
                    "face_index": face_key[2],
                },
            )
            feature_row = _reference_row(
                reference=(4, 5, 6),
                target={
                    "kind": "feature",
                    "component": COMPONENT,
                    "name": "Boss-Extrude1",
                    "type_name": "Extrusion",
                    "faces": [
                        {
                            "body_type": face_key[0],
                            "body_index": face_key[1],
                            "face_index": face_key[2],
                        }
                    ],
                },
            )
            face["appearance"]["native_identity"] = {
                "persistent_reference": face_row["persistent_reference"],
                "conversion": face_row["conversion"],
                "read_errors": {},
                "feature": {
                    "name": "Boss-Extrude1",
                    "type_name": "Extrusion",
                    "persistent_reference": feature_row[
                        "persistent_reference"
                    ],
                    "conversion": feature_row["conversion"],
                    "read_errors": {},
                },
            }
    return raw


def _attach_body_witnesses(raw, references=None):
    references = references or {}
    for body in raw["bodies"]:
        observations = []
        distinct_references = set()
        for face_position, face in enumerate(body["faces"]):
            face_key = (
                body["body_type"],
                body["body_index"],
                face["face_index"],
            )
            face_reference = [101, face_position]
            feature_reference = [201, face_position]
            face_row = _reference_row(
                reference=face_reference,
                target={
                    "kind": "face",
                    "component": COMPONENT,
                    "body_type": face_key[0],
                    "body_index": face_key[1],
                    "face_index": face_key[2],
                },
            )
            feature_row = _reference_row(
                reference=feature_reference,
                target={
                    "kind": "feature",
                    "component": COMPONENT,
                    "name": "Boss-Extrude1",
                    "type_name": "Extrusion",
                    "faces": [
                        {
                            "body_type": face_key[0],
                            "body_index": face_key[1],
                            "face_index": face_key[2],
                        }
                    ],
                },
            )
            face["appearance"]["native_identity"] = {
                "persistent_reference": face_reference,
                "conversion": face_row["conversion"],
                "read_errors": {},
                "feature": {
                    "name": "Boss-Extrude1",
                    "type_name": "Extrusion",
                    "persistent_reference": feature_reference,
                    "conversion": feature_row["conversion"],
                    "read_errors": {},
                },
            }
            reference = list(references.get(face_key, (1, 2, 3)))
            distinct_references.add(tuple(reference))
            observations.append(
                {
                    "face_index": face["face_index"],
                    "persistent_reference": reference,
                    "conversion": {
                        "component": raw["component"],
                        "input_context": {
                            "owner_scope": "assembly_occurrence",
                            "object_kind": "assembly_occurrence",
                            "source_api": (
                                "IComponent2.GetBodies3/IBody2.GetFaces"
                            ),
                            "traversal_api": (
                                "IConfiguration.GetRootComponent3/"
                                "IComponent2.GetChildren"
                            ),
                        },
                        "receiver": {
                            "owner_scope": "component_document",
                            "owner_document_path": DOCUMENT,
                            "interface": "IModelDocExtension",
                            "object_source": (
                                "captured_component_source_document"
                            ),
                        },
                        "api": (
                            "IFace2.GetBody+"
                            "IModelDocExtension.GetPersistReference3"
                        ),
                        "output_interface_kind": "IBody2",
                        "output_domain": {
                            "proof_api": (
                                "IModelDocExtension."
                                "GetObjectByPersistReference3"
                            ),
                            "status": 0,
                            "interface_kind": "IBody2",
                            "expected_owner_document_path": DOCUMENT,
                        },
                        "state": "observed",
                        "read_errors": {},
                    },
                    "read_errors": {},
                }
            )
        body["native_identity"] = {
            "owner_scope": "component_document",
            "owner_document_path": DOCUMENT,
            "state": "observed",
            "part_body_reference_count": len(distinct_references),
            "read_errors": {},
            "observation_errors": [],
            "face_body_observations": observations,
        }
    return raw


class CaptureAppearanceBindingTests(unittest.TestCase):

    def test_consumers_bind_appearance_and_scope_from_native_facts(self):
        appearance = _raw()
        raw_scene = {"components": [{"name": COMPONENT}]}
        geometry = [{"component": COMPONENT, "path": "geometry/arm.stl"}]
        scope = {
            "source_document_path": DOCUMENT,
            "referenced_configuration": CONFIGURATION,
        }
        bound_scene, bound_geometry = bind_capture_appearance(
            raw_scene,
            geometry,
            {
                "scene_raw": {
                    "occurrences": [
                        {
                            "name": COMPONENT,
                            "appearance_raw": appearance,
                        }
                    ]
                },
                "source_scopes": {COMPONENT: scope},
            },
        )
        self.assertEqual(
            bound_scene["components"][0]["appearance_raw"],
            appearance,
        )
        self.assertEqual(bound_geometry[0]["source_scope"], scope)
        self.assertNotIn("appearance_raw", raw_scene["components"][0])
        self.assertNotIn("source_scope", geometry[0])


class AppearanceResolutionTests(unittest.TestCase):
    def test_part_conversion_evidence_is_required_and_target_specific(self):
        case = json.loads(PART_FACE_OVERRIDE_FIXTURE.read_text(encoding="utf-8"))
        raw = copy.deepcopy(case["raw"])
        indexed = _reference_row(
            reference=(1, 2, 3),
            target={
                "kind": "face",
                "component": COMPONENT,
                "body_type": 0,
                "body_index": 0,
                "face_index": 0,
            },
        )
        _validate_index_conversion(
            COMPONENT,
            {
                **indexed,
                "target": indexed["target"],
            },
            owner_document_path=DOCUMENT,
            path="capture",
        )
        for target_kind, api, interface, source_api in (
            (
                "body",
                "IFace2.GetBody+"
                "IModelDocExtension.GetPersistReference3",
                "IBody2",
                "IComponent2.GetBodies3/IBody2.GetFaces",
            ),
            (
                "face",
                "IModelDocExtension.GetCorrespondingEntity2",
                "IFace2",
                "IComponent2.GetBodies3/IBody2.GetFaces",
            ),
            (
                "feature",
                "IFace2.GetFeature+"
                "IModelDocExtension.GetPersistReference3",
                "IFeature",
                "IComponent2.GetBodies3/IBody2.GetFaces",
            ),
        ):
            target = copy.deepcopy(indexed["target"])
            target["kind"] = target_kind
            if target_kind == "body":
                target["persistent_reference_sha256"] = hashlib.sha256(
                    bytes(
                        item & 0xFF
                        for item in indexed["persistent_reference"]
                    )
                ).hexdigest()
                target["faces"] = [
                    {
                        "body_type": target.pop("body_type"),
                        "body_index": target.pop("body_index"),
                        "face_index": target.pop("face_index", 0),
                    }
                ]
                target["occurrence_bodies"] = [[0, 0]]
            elif target_kind == "feature":
                target.update(
                    {
                        "name": "Boss-Extrude1",
                        "type_name": "Extrusion",
                        "persistent_reference": list(
                            indexed["persistent_reference"]
                        ),
                        "faces": [
                            {
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                            }
                        ],
                    }
                )
                target.pop("face_index", None)
            conversion = copy.deepcopy(indexed["conversion"])
            indexed["native_interface_kind"] = (
                "IBody2"
                if target_kind == "body"
                else "IEntity"
                if target_kind == "face"
                else None
            )
            conversion.update(
                {
                    "api": api,
                    "output_interface_kind": interface,
                }
            )
            conversion["output_domain"]["interface_kind"] = interface
            conversion["input_context"]["source_api"] = source_api
            valid = {**indexed, "target": target, "conversion": conversion}
            _validate_index_conversion(
                COMPONENT,
                valid,
                owner_document_path=DOCUMENT,
                path="capture",
            )
            for mutation in (
                lambda value: value.pop("conversion"),
                lambda value: value["conversion"].update(
                    receiver={
                        **value["conversion"]["receiver"],
                        "owner_scope": "top_assembly",
                    }
                ),
                lambda value: value["conversion"]["receiver"].update(
                    owner_document_path=r"C:\capture\other.SLDPRT"
                ),
                lambda value: value["conversion"].update(
                    api="IComponent2.GetCorrespondingEntity"
                ),
                lambda value, kind=target_kind: value["conversion"].update(
                    output_interface_kind=(
                        "IFace2"
                        if kind == "body"
                        else "IBody2"
                    )
                ),
                lambda value: value["conversion"]["output_domain"].update(
                    proof_api="ISldWorks.IsSame",
                    status=1,
                ),
                lambda value: value["conversion"]["output_domain"].update(
                    status=False
                ),
            ):
                malformed = copy.deepcopy(valid)
                mutation(malformed)
                with (
                    self.subTest(target_kind=target_kind),
                    self.assertRaises(AppearanceError) as caught,
                ):
                    _validate_index_conversion(
                        COMPONENT,
                        malformed,
                        owner_document_path=DOCUMENT,
                        path="capture",
                    )
                self.assertEqual(
                    caught.exception.appearance["issues"][0]["code"],
                    "cad_appearance_render_conversion_unproven",
                )
            if target_kind == "body":
                malformed = copy.deepcopy(valid)
                malformed["target"]["persistent_reference_sha256"] = "0" * 64
                with self.assertRaises(AppearanceError):
                    _validate_index_conversion(
                        COMPONENT,
                        malformed,
                        owner_document_path=DOCUMENT,
                        path="capture",
                    )
        malformed = copy.deepcopy(raw)
        malformed["bodies"][0]["faces"][0]["appearance"]["native_identity"][
            "conversion"
        ]["receiver"]["owner_scope"] = "top_assembly"
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                malformed,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=case["face_spans"],
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_conversion_unproven",
        )

    def test_part_material_precedes_only_lower_scopes(self):
        case = json.loads(PART_FACE_OVERRIDE_FIXTURE.read_text(encoding="utf-8"))
        resolved = resolve_deliverable_appearance(
            copy.deepcopy(case["raw"]),
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=case["face_spans"],
        )
        self.assertEqual(
            {face.face_index: face.rgba for face in resolved.faces},
            {
                0: (0.0, 0.0, 1.0, 1.0),
                1: (1.0, 0.0, 0.0, 1.0),
            },
        )
        self.assertEqual(
            {
                face.face_index: face.source["scopes"]
                for face in resolved.faces
            },
            {0: ["face"], 1: ["document"]},
        )

    def test_same_scope_color_encodings_share_one_observed_rgba(self):
        for owner_red, should_pass in ((57 / 255.0, True), (58 / 255.0, False)):
            with self.subTest(owner_red=owner_red):
                owner_channel = (
                    math.nextafter(owner_red, -math.inf)
                    if should_pass
                    else owner_red
                )
                raw = _resolved_raw(
                    [
                        [
                            owner_channel,
                            owner_channel,
                            owner_channel,
                            1.0,
                            1.0,
                            0.5,
                            0.3125,
                            0.0,
                            0.0,
                        ]
                    ]
                )
                owner = _empty_state_owner(
                    scope="component_document",
                    document_path=DOCUMENT,
                    configuration=CONFIGURATION,
                    display_state="Display State 1",
                )
                owner["call_state"] = "observed"
                owner["native_render_material_count"] = 1
                owner["native_reference_index"] = []
                owner["materials"] = [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Display State 1"],
                        "entities": ["face"],
                        "entities_state": "observed",
                        "entity_records": [
                            _state_entity_record(kind="entity", reference=(1, 2, 3))
                        ],
                        "primary_color": 0x393939,
                        "transparency": 0.0,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ]
                raw["state_render_materials"] = [owner]
                spans = [
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "face_index": 0,
                        "triangle_start": 0,
                        "triangle_count": 1,
                        "face_reference_sha256": "a" * 64,
                    }
                ]
                if should_pass:
                    resolved = resolve_deliverable_appearance(
                        raw,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                        face_spans=spans,
                    )
                    self.assertEqual(
                        resolved.faces[0].rgba,
                        (
                            owner_channel,
                            owner_channel,
                            owner_channel,
                            1.0,
                        ),
                    )
                    self.assertEqual(
                        {source["kind"] for source in resolved.faces[0].source["owners"]},
                        {"owner", "display_state_render_material"},
                    )
                else:
                    with self.assertRaises(AppearanceError) as caught:
                        resolve_deliverable_appearance(
                            raw,
                            component=COMPONENT,
                            expected_document_path=DOCUMENT,
                            expected_configuration=CONFIGURATION,
                            face_spans=spans,
                        )
                    self.assertEqual(
                        caught.exception.appearance["issues"][0]["code"],
                        "cad_appearance_owner_conflict",
                    )

    def test_body_target_uses_its_exact_face_footprint(self):
        raw = _resolved_raw([None, None])
        _attach_body_witnesses(raw)
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["native_reference_index"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["body"],
                "entities_state": "observed",
                "entity_records": [
                    _state_entity_record(
                        reference=(1, 2, 3),
                        native_interface_kinds=["IBody2"],
                    )
                ],
                "primary_color": 0x0000FF00,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        raw["state_render_materials"] = [owner]
        spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "face_index": face_index,
                "triangle_start": face_index,
                "triangle_count": 1,
                "face_reference_sha256": str(face_index + 1) * 64,
            }
            for face_index in (0, 1)
        ]

        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=spans,
        )
        self.assertEqual(
            {face.rgba for face in resolved.faces},
            {(0.0, 1.0, 0.0, 1.0)},
        )
        self.assertEqual(
            {tuple(face.source["scopes"]) for face in resolved.faces},
            {("body",)},
        )

        for label in ("wrong_component", "owner", "api", "interface"):
            with self.subTest(label=label):
                malformed_observation = copy.deepcopy(raw)
                body = malformed_observation["bodies"][0]
                conversion = body["native_identity"][
                    "face_body_observations"
                ][1]["conversion"]
                if label == "wrong_component":
                    body["component"] = "other-1"
                elif label == "owner":
                    conversion["receiver"][
                        "owner_document_path"
                    ] = "C:/cad/other.SLDPRT"
                elif label == "api":
                    conversion["api"] = "IModelDocExtension.GetCorresponding2"
                else:
                    conversion["output_interface_kind"] = "IFace2"
                with self.assertRaises(AppearanceError) as malformed_caught:
                    resolve_deliverable_appearance(
                        malformed_observation,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                        face_spans=spans,
                    )
                self.assertIn(
                    malformed_caught.exception.appearance["issues"][0][
                        "code"
                    ],
                    {
                        "cad_appearance_body_identity_invalid",
                        "cad_appearance_render_entity_binding_unproven",
                        "cad_appearance_render_identity_unresolved",
                    },
                )

        duplicate_body = copy.deepcopy(raw)
        duplicate = copy.deepcopy(duplicate_body["bodies"][0])
        duplicate["native_identity"]["face_body_observations"][0][
            "persistent_reference"
        ] = [4, 5, 6]
        duplicate_body["bodies"].append(duplicate)
        with self.assertRaises(AppearanceError) as duplicate_caught:
            resolve_deliverable_appearance(
                duplicate_body,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=spans,
            )
        self.assertEqual(
            duplicate_caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_body_identity_invalid",
        )

        no_reference_fragment = copy.deepcopy(raw)
        failed_body = copy.deepcopy(no_reference_fragment["bodies"][0])
        failed_body["body_index"] = 1
        failed_body["faces"] = [copy.deepcopy(failed_body["faces"][0])]
        failed_body["native_identity"] = {
            "owner_scope": "component_document",
            "owner_document_path": DOCUMENT,
            "state": "error",
            "part_body_reference_count": 0,
            "read_errors": {
                "body_reference": {
                    "code": "cad_appearance_part_body_missing"
                }
            },
            "observation_errors": [
                {
                    "face_index": 0,
                    "read_errors": {
                        "body": {
                            "code": "cad_appearance_part_body_missing"
                        }
                    },
                }
            ],
            "face_body_observations": [
                {
                    "face_index": 0,
                    "persistent_reference": None,
                    "conversion": {
                        "state": "error",
                        "read_errors": {
                            "body": {
                                "code": "cad_appearance_part_body_missing"
                            }
                        },
                    },
                    "read_errors": {
                        "body": {
                            "code": "cad_appearance_part_body_missing"
                        }
                    },
                }
            ],
        }
        no_reference_fragment["bodies"].append(failed_body)
        failed_spans = [
            *spans,
            {
                "body_type": 0,
                "body_index": 1,
                "face_index": 0,
                "triangle_start": 2,
                "triangle_count": 1,
                "face_reference_sha256": "3" * 64,
            },
        ]
        with self.assertRaises(AppearanceError) as failed_fragment_caught:
            resolve_deliverable_appearance(
                no_reference_fragment,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=failed_spans,
            )
        self.assertEqual(
            failed_fragment_caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_identity_unresolved",
        )

        wrong_interface = copy.deepcopy(raw)
        wrong_entity = wrong_interface["state_render_materials"][0][
            "materials"
        ][0]["entity_records"][0]
        wrong_entity["native_interface_kinds"] = ["IFeature"]
        wrong_entity["native_identity"]["interface_kinds"] = ["IFeature"]
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                wrong_interface,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=spans,
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_entity_interface_mismatch",
        )

        cross_fragment = _resolved_raw([None, None])
        second_body = copy.deepcopy(cross_fragment["bodies"][0])
        second_body["body_index"] = 1
        second_body["faces"] = [copy.deepcopy(second_body["faces"][0])]
        cross_fragment["bodies"].append(second_body)
        _attach_body_witnesses(cross_fragment)
        conflicting_observation = copy.deepcopy(
            cross_fragment["bodies"][0]["native_identity"][
                "face_body_observations"
            ][0]
        )
        conflicting_observation["persistent_reference"] = [4, 5, 6]
        cross_fragment["bodies"][0]["native_identity"][
            "face_body_observations"
        ].append(conflicting_observation)
        cross_fragment["bodies"][0]["native_identity"][
            "part_body_reference_count"
        ] = 2
        cross_owner = copy.deepcopy(raw["state_render_materials"][0])
        cross_owner["native_reference_index"] = []
        cross_owner["mapping_errors"] = []
        cross_fragment["state_render_materials"] = [cross_owner]
        cross_spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "face_index": face_index,
                "triangle_start": face_index,
                "triangle_count": 1,
                "face_reference_sha256": str(face_index + 1) * 64,
            }
            for face_index in (0, 1)
        ]
        cross_spans.append(
            {
                "body_type": 0,
                "body_index": 1,
                "face_index": 0,
                "triangle_start": 2,
                "triangle_count": 1,
                "face_reference_sha256": "3" * 64,
            }
        )
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                cross_fragment,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=cross_spans,
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_identity_unresolved",
        )

    def test_body_target_accepts_legal_multi_body_and_cross_fragment_ownership(
        self,
    ):
        def attach_material(raw):
            owner = _empty_state_owner(
                scope="component_document",
                document_path=DOCUMENT,
                configuration=CONFIGURATION,
                display_state="Display State 1",
            )
            owner["call_state"] = "observed"
            owner["native_render_material_count"] = 1
            owner["native_reference_index"] = []
            owner["materials"] = [
                {
                    "material_index": 0,
                    "linked_display_states": ["Display State 1"],
                    "entities": ["body"],
                    "entities_state": "observed",
                    "entity_records": [
                        _state_entity_record(
                            reference=(1, 2, 3),
                            native_interface_kinds=["IBody2"],
                        )
                    ],
                    "primary_color": 0x0000FF00,
                    "transparency": 0.0,
                    "color_form": 1,
                    "texture_filename": "",
                    "surface_finish_file": "",
                    "read_errors": {},
                }
            ]
            raw["state_render_materials"] = [owner]
            return raw

        multi_body = _resolved_raw([None, None])
        _attach_body_witnesses(
            multi_body,
            {
                (0, 0, 0): (1, 2, 3),
                (0, 0, 1): (4, 5, 6),
            },
        )
        attach_material(multi_body)
        multi_owner = multi_body["state_render_materials"][0]
        second_material = copy.deepcopy(multi_owner["materials"][0])
        second_material["material_index"] = 1
        second_material["primary_color"] = 0x000000FF
        second_material["entity_records"][0][
            "persistent_reference"
        ] = [4, 5, 6]
        second_material["entity_records"][0]["native_identity"][
            "interface_kinds"
        ] = ["IBody2"]
        multi_owner["materials"].append(second_material)
        multi_owner["native_render_material_count"] = 2
        multi_spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "face_index": face_index,
                "triangle_start": face_index,
                "triangle_count": 1,
                "face_reference_sha256": str(face_index + 1) * 64,
            }
            for face_index in (0, 1)
        ]
        multi_resolved = resolve_deliverable_appearance(
            multi_body,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=multi_spans,
        )
        self.assertEqual(
            next(
                face
                for face in multi_resolved.faces
                if face.face_index == 0
            ).rgba,
            (0.0, 1.0, 0.0, 1.0),
        )
        self.assertEqual(
            next(
                face
                for face in multi_resolved.faces
                if face.face_index == 1
            ).rgba,
            (1.0, 0.0, 0.0, 1.0),
        )

        cross_fragment = _resolved_raw([None, None])
        second_body = copy.deepcopy(cross_fragment["bodies"][0])
        second_body["body_index"] = 1
        second_body["faces"] = [copy.deepcopy(second_body["faces"][0])]
        cross_fragment["bodies"].append(second_body)
        _attach_body_witnesses(cross_fragment)
        attach_material(cross_fragment)
        cross_spans = [
            *multi_spans,
            {
                "body_type": 0,
                "body_index": 1,
                "face_index": 0,
                "triangle_start": 2,
                "triangle_count": 1,
                "face_reference_sha256": "3" * 64,
            },
        ]
        cross_resolved = resolve_deliverable_appearance(
            cross_fragment,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=cross_spans,
        )
        self.assertEqual(
            {tuple(face.source["scopes"]) for face in cross_resolved.faces},
            {("body",)},
        )

    def test_assembly_occurrence_override_precedes_part_and_face(self):
        case = json.loads(PART_FACE_OVERRIDE_FIXTURE.read_text(encoding="utf-8"))
        raw = copy.deepcopy(case["raw"])
        top = _empty_state_owner(
            scope="top_assembly",
            document_path=r"C:\capture\robot.SLDASM",
            configuration="Top Configuration",
            display_state="Top State",
            display_states=["Top State"],
            display_states_state="observed",
        )
        top["call_state"] = "observed"
        top["native_render_material_count"] = 1
        top["native_reference_index"] = [
            _reference_row(
                reference=(4, 5, 6),
                target={"kind": "occurrence", "component": COMPONENT},
                owner_scope="top_assembly",
                owner_document_path=r"C:\capture\robot.SLDASM",
            )
        ]
        top["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Top State"],
                "entities": ["top-occurrence"],
                "entities_state": "observed",
                "entity_records": [
                    _state_entity_record(
                        kind="object",
                        reference=(4, 5, 6),
                        owner_scope="top_assembly",
                        owner_document_path=r"C:\capture\robot.SLDASM",
                        native_interface_kinds=["IComponent2"],
                    )
                ],
                "primary_color": 0x0000FF00,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        raw["state_render_materials"].insert(0, top)
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=case["face_spans"],
        )
        self.assertEqual(
            {face.rgba for face in resolved.faces},
            {(0.0, 1.0, 0.0, 1.0)},
        )
        self.assertEqual(
            {tuple(face.source["scopes"]) for face in resolved.faces},
            {("occurrence",)},
        )
        non_occurrence = copy.deepcopy(raw)
        entity = non_occurrence["state_render_materials"][0]["materials"][
            0
        ]["entity_records"][0]
        entity["persistent_reference"] = [7, 8, 9]
        entity["native_interface_kinds"] = ["IFace2", "IEntity"]
        entity["native_identity"] = {
            "kind": "reference",
            "state": "observed",
            "proof_api": "IModelDocExtension.GetObjectByPersistReference3",
            "status": 0,
            "owner_document_path": DOCUMENT,
            "interface_kinds": ["IFace2", "IEntity"],
            "read_errors": {},
        }
        non_occurrence["state_render_materials"][0][
            "native_reference_index"
        ] = [
            _reference_row(
                reference=(7, 8, 9),
                target={
                    "kind": "face",
                    "component": COMPONENT,
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                },
                owner_scope="top_assembly",
                owner_document_path=r"C:\capture\robot.SLDASM",
            )
        ]
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                non_occurrence,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=case["face_spans"],
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_entity_invalid",
        )

    def test_face_owners_partition_one_occurrence_into_multiple_materials(self):
        red = [1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.25, 0.0]
        blue = [0.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]
        spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "face_index": index,
                "triangle_start": index * 2,
                "triangle_count": 2,
                "face_reference_sha256": str(index + 1) * 64,
            }
            for index in range(2)
        ]
        resolution = resolve_deliverable_appearance(
            _resolved_raw([red, blue]),
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=spans,
        )
        groups = appearance_groups(resolution.faces)
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {tuple(group["rgba"]) for group in groups},
            {
                (1.0, 0.0, 0.0, 0.75),
                (0.0, 0.0, 1.0, 1.0),
            },
        )
        self.assertTrue(
            all(
                group["triangle_ranges"]
                == [{"triangle_start": index * 2, "triangle_count": 2}]
                for index, group in enumerate(
                    sorted(groups, key=lambda row: row["face_references"][0])
                )
            )
        )
        source = support.binary_stl_bytes(
            [((0, 0, 0), (1, 0, 0), (0, 1, 0))] * 4
        )
        subsets = [
            native_stl_subset(source, group["triangle_ranges"])
            for group in groups
        ]
        self.assertEqual(
            sum(count for _payload, count in subsets),
            4,
        )
        self.assertTrue(all(count == 2 for _payload, count in subsets))

    def test_documented_face_override_wins_over_part_default(self):
        red = [1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]
        blue = [0.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]
        raw = _resolved_raw([red])
        raw["document"] = _layer(
            owner_configuration=CONFIGURATION,
            values=blue,
            has_values=True,
        )
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(resolved.faces[0].rgba, (1.0, 0.0, 0.0, 1.0))
        self.assertEqual(resolved.faces[0].source["scopes"], ["face"])

    def test_top_parent_and_leaf_keep_their_own_display_states(self):
        raw = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        raw["state_render_materials"] = [
            _empty_state_owner(
                scope="top_assembly",
                document_path=r"C:\capture\robot.SLDASM",
                configuration="Top Configuration",
                display_state="Top State",
                display_states=["Top State", "Other Top State"],
                display_states_state="observed",
            ),
            _empty_state_owner(
                scope="parent_assembly",
                document_path=r"C:\capture\parent.SLDASM",
                configuration="Parent Configuration",
                display_state="Parent State",
            ),
            _empty_state_owner(
                scope="component_document",
                document_path=DOCUMENT,
                configuration=CONFIGURATION,
                display_state="Display State 1",
            ),
        ]
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(resolved.faces[0].source["scopes"], ["face"])

    def test_material_linked_state_is_compared_with_its_owner_state(self):
        raw = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        top = _empty_state_owner(
            scope="top_assembly",
            document_path=r"C:\capture\robot.SLDASM",
            configuration="Top Configuration",
            display_state="Top State",
            display_states=["Top State", "Other Top State"],
            display_states_state="observed",
        )
        top["call_state"] = "observed"
        top["native_render_material_count"] = 1
        top["native_reference_index"] = [
            _reference_row(
                reference=(4, 5, 6),
                target={"kind": "occurrence", "component": COMPONENT},
                owner_scope="top_assembly",
                owner_document_path=r"C:\capture\robot.SLDASM",
            )
        ]
        top["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Top State"],
                "entities": ["top-face"],
                "entities_state": "observed",
                "entity_records": [
                    _state_entity_record(
                        kind="object",
                        reference=(4, 5, 6),
                        owner_scope="top_assembly",
                        owner_document_path=r"C:\capture\robot.SLDASM",
                        native_interface_kinds=["IComponent2"],
                    )
                ],
                "primary_color": 0x00AABBCC,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        raw["state_render_materials"] = [
            top,
            _empty_state_owner(
                scope="component_document",
                document_path=DOCUMENT,
                configuration=CONFIGURATION,
                display_state="Display State 1",
            ),
        ]
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(
            resolved.faces[0].rgba,
            (0xCC / 255, 0xBB / 255, 0xAA / 255, 1.0),
        )
        self.assertEqual(
            resolved.faces[0].source["owners"][0]["derivation"][
                "rule_id"
            ],
            "appearance.primary_color_colorref",
        )

    def test_primary_color_colorref_known_answers(self):
        cases = (
            (0x000000FF, 0.0, (1.0, 0.0, 0.0, 1.0)),
            (0x0000FF00, 0.0, (0.0, 1.0, 0.0, 1.0)),
            (0x00FF0000, 0.0, (0.0, 0.0, 1.0, 1.0)),
            (
                0x00332211,
                0.25,
                (0x11 / 255, 0x22 / 255, 0x33 / 255, 0.75),
            ),
        )
        for primary_color, transparency, expected in cases:
            with self.subTest(primary_color=hex(primary_color)):
                self.assertEqual(
                    _rgba_from_colorref(primary_color, transparency),
                    expected,
                )

    def test_missing_body_owner_is_absence_not_a_malformed_record(self):
        raw = _resolved_raw([None])
        raw["bodies"][0].pop("appearance")
        raw["bodies"][0]["faces"][0]["appearance"] = _layer(
            owner_configuration=CONFIGURATION
        )
        raw["document"] = _layer(
            owner_configuration=CONFIGURATION,
            values=[0.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0],
            has_values=True,
        )
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(resolved.faces[0].source["scopes"], ["document"])

    def test_zero_native_count_proves_null_state_materials_are_empty(self):
        raw = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "null_result"
        owner["materials"] = None
        raw["state_render_materials"] = [owner]
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(resolved.faces[0].source["scopes"], ["face"])

    def test_component_owner_requires_no_derived_footprint_authority(self):
        mapping_error = {
            "owner_scope": "component_document",
            "owner_document_path": DOCUMENT,
            "component": COMPONENT,
            "entity_kind": "faces",
            "target_kind": "face",
            "identity": {
                "component": COMPONENT,
                "body_type": 0,
                "body_index": 0,
                "face_index": 0,
            },
            "persistent_reference": None,
            "native_interface_kind": "IEntity",
            "error": {"code": "cad_appearance_persistent_reference_invalid"},
        }
        unrelated_error = copy.deepcopy(mapping_error)
        unrelated_error["identity"]["face_index"] = 1
        empty = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        resolved_empty = resolve_deliverable_appearance(
            empty,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(resolved_empty.faces[0].source["scopes"], ["face"])
        for field, value in (
            ("mapping_errors", [unrelated_error]),
            ("native_reference_index", [{"derived": "footprint"}]),
        ):
            with self.subTest(field=field):
                malformed = copy.deepcopy(empty)
                malformed["state_render_materials"][0][field] = value
                with self.assertRaises(AppearanceError) as malformed_caught:
                    resolve_deliverable_appearance(
                        malformed,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                        face_spans=[
                            {
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                                "triangle_start": 0,
                                "triangle_count": 1,
                                "face_reference_sha256": "a" * 64,
                            }
                        ],
                    )
                self.assertEqual(
                    malformed_caught.exception.appearance["issues"][0][
                        "code"
                    ],
                    "cad_appearance_render_owner_invalid",
                )

        object_raw = _resolved_raw(
            [
                None,
                [0.0, 1.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0],
            ]
        )
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["mapping_errors"] = []
        owner["native_reference_index"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["face"],
                "entities_state": "observed",
                "entity_records": [
                    _state_entity_record(
                        kind="entity",
                        reference=(1, 2, 3),
                    )
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        object_raw["state_render_materials"] = [owner]
        resolved_object = resolve_deliverable_appearance(
            object_raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                },
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 1,
                    "triangle_start": 1,
                    "triangle_count": 1,
                    "face_reference_sha256": "b" * 64,
                },
            ],
        )
        self.assertEqual(resolved_object.faces[0].source["scopes"], ["face"])

        document = _resolved_raw([None])
        document["document"] = _layer(
            owner_configuration=CONFIGURATION,
            values=[0.0, 0.0, 1.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0],
            has_values=True,
        )
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["mapping_errors"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["document"],
                "entities_state": "observed",
                "entity_records": [
                    {
                        "entity_index": 0,
                        "native_object_type": "IModelDoc2",
                        "native_interface_kinds": ["IModelDoc2"],
                        "persistent_reference": None,
                        "binding_context": _binding_context(
                            owner_scope="component_document",
                            owner_document_path=DOCUMENT,
                            api=(
                                "IModelDoc2.GetPathName"
                                "+ConfigurationManager.ActiveConfiguration.Name"
                            ),
                        ),
                        "native_identity": {
                            "kind": "document",
                            "qi_iid": "{B90793FB-EF3D-4B80-A5C4-99959CDB6CEB}",
                            "document_path": DOCUMENT,
                            "active_configuration": CONFIGURATION,
                            "state": "observed",
                            "read_errors": {},
                        },
                        "read_errors": {},
                    }
                ],
                "primary_color": 0x00AABBCC,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        document["state_render_materials"] = [owner]
        resolved_document = resolve_deliverable_appearance(
            document,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(
            resolved_document.faces[0].source["scopes"],
            ["document"],
        )

    def test_nonzero_or_unknown_count_keeps_null_state_materials_unresolved(self):
        for count, count_error in (
            (1, None),
            (None, {"code": "cad_appearance_render_material_count_unreadable"}),
        ):
            with self.subTest(count=count):
                raw = _resolved_raw(
                    [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
                )
                owner = _empty_state_owner(
                    scope="component_document",
                    document_path=DOCUMENT,
                    configuration=CONFIGURATION,
                    display_state="Display State 1",
                )
                owner["call_state"] = "null_result"
                owner["materials"] = None
                owner["native_render_material_count"] = count
                owner["native_render_material_count_error"] = count_error
                if count_error is not None:
                    owner["read_errors"] = {
                        "native_render_material_count": count_error
                    }
                raw["state_render_materials"] = [owner]
                with self.assertRaises(AppearanceError) as caught:
                    resolve_deliverable_appearance(
                        raw,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                        face_spans=[
                            {
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                                "triangle_start": 0,
                                "triangle_count": 1,
                                "face_reference_sha256": "a" * 64,
                            }
                        ],
                    )
                self.assertEqual(
                    caught.exception.appearance["issues"][0]["code"],
                    "cad_appearance_render_enumeration_failed",
                )

    def test_observed_material_row_count_must_match_native_count(self):
        raw = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 2
        owner["native_reference_index"] = []
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["entity"],
                "entities_state": "observed",
                "entity_records": [
                    _state_entity_record(
                        kind="object",
                        reference=(1, 2, 3),
                        native_interface_kinds=["IComponent2"],
                    )
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        raw["state_render_materials"] = [owner]
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                raw,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=[
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "face_index": 0,
                        "triangle_start": 0,
                        "triangle_count": 1,
                        "face_reference_sha256": "a" * 64,
                    }
                ],
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_material_count_mismatch",
        )

    def test_same_reference_alias_cannot_expand_to_two_face_targets(self):
        raw = _resolved_raw([None, None])
        raw["document"] = _layer(
            owner_configuration=CONFIGURATION,
            values=[0.0, 1.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0],
            has_values=True,
        )
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        owner["native_reference_index"] = []
        raw["bodies"][0]["faces"][1]["appearance"]["native_identity"][
            "persistent_reference"
        ] = [1, 2, 3]
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["ambiguous"],
                "entities_state": "observed",
                "entity_records": [
                    _state_entity_record(
                        kind="entity",
                        reference=(1, 2, 3),
                    )
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        raw["state_render_materials"] = [owner]
        with self.assertRaises(AppearanceError) as caught:
            resolve_deliverable_appearance(
                raw,
                component=COMPONENT,
                expected_document_path=DOCUMENT,
                expected_configuration=CONFIGURATION,
                face_spans=[
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "face_index": face_index,
                        "triangle_start": face_index,
                        "triangle_count": 1,
                        "face_reference_sha256": str(face_index + 1) * 64,
                    }
                    for face_index in (0, 1)
                ],
            )
        self.assertEqual(
            caught.exception.appearance["issues"][0]["code"],
            "cad_appearance_render_entity_ambiguous",
        )

    def test_feature_alias_merges_only_one_native_feature_footprint(self):
        for feature_names, should_pass in (
            (("Boss-Extrude1", "Boss-Extrude1"), True),
            (("Boss-Extrude1", "Cut-Extrude1"), False),
        ):
            with self.subTest(feature_names=feature_names):
                raw = _resolved_raw([None, None])
                raw["document"] = _layer(
                    owner_configuration=CONFIGURATION,
                    values=[
                        0.0,
                        1.0,
                        0.0,
                        1.0,
                        1.0,
                        0.5,
                        0.3125,
                        0.0,
                        0.0,
                    ],
                    has_values=True,
                )
                owner = _empty_state_owner(
                    scope="component_document",
                    document_path=DOCUMENT,
                    configuration=CONFIGURATION,
                    display_state="Display State 1",
                )
                owner["call_state"] = "observed"
                owner["native_render_material_count"] = 1
                owner["native_reference_index"] = []
                if not should_pass:
                    raw["bodies"][0]["faces"][1]["appearance"][
                        "native_identity"
                    ]["feature"]["name"] = feature_names[1]
                owner["materials"] = [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Display State 1"],
                        "entities": ["feature"],
                        "entities_state": "observed",
                        "entity_records": [
                            _state_entity_record(
                                kind="object",
                                reference=(4, 5, 6),
                                native_interface_kinds=["IFeature"],
                            )
                        ],
                        "primary_color": 0x000000FF,
                        "transparency": 0.0,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ]
                raw["state_render_materials"] = [owner]
                spans = [
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "face_index": face_index,
                        "triangle_start": face_index,
                        "triangle_count": 1,
                        "face_reference_sha256": str(face_index + 1) * 64,
                    }
                    for face_index in range(2)
                ]
                if should_pass:
                    resolved = resolve_deliverable_appearance(
                        raw,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                        face_spans=spans,
                    )
                    self.assertEqual(
                        {face.rgba for face in resolved.faces},
                        {(1.0, 0.0, 0.0, 1.0)},
                    )
                    incomplete = copy.deepcopy(raw)
                    incomplete["bodies"][0]["faces"][0]["appearance"][
                        "native_identity"
                    ]["feature"]["read_errors"] = {
                        "name": {
                            "code": "cad_member_missing",
                            "message": "missing",
                            "detail": {},
                        }
                    }
                    with self.assertRaises(AppearanceError) as caught:
                        resolve_deliverable_appearance(
                            incomplete,
                            component=COMPONENT,
                            expected_document_path=DOCUMENT,
                            expected_configuration=CONFIGURATION,
                            face_spans=spans,
                        )
                    self.assertEqual(
                        caught.exception.appearance["issues"][0]["code"],
                        "cad_appearance_render_identity_unresolved",
                    )
                    missing_feature_reference = copy.deepcopy(raw)
                    missing_feature_reference["bodies"][0]["faces"][0][
                        "appearance"
                    ]["native_identity"]["feature"][
                        "persistent_reference"
                    ] = None
                    missing_feature_reference["bodies"][0]["faces"][0][
                        "appearance"
                    ]["native_identity"]["feature"]["conversion"] = None
                    with self.assertRaises(AppearanceError) as caught:
                        resolve_deliverable_appearance(
                            missing_feature_reference,
                            component=COMPONENT,
                            expected_document_path=DOCUMENT,
                            expected_configuration=CONFIGURATION,
                            face_spans=spans,
                        )
                    self.assertEqual(
                        caught.exception.appearance["issues"][0]["code"],
                        "cad_appearance_render_identity_unresolved",
                    )
                else:
                    with self.assertRaises(AppearanceError) as caught:
                        resolve_deliverable_appearance(
                            raw,
                            component=COMPONENT,
                            expected_document_path=DOCUMENT,
                            expected_configuration=CONFIGURATION,
                            face_spans=spans,
                        )
                    self.assertEqual(
                        caught.exception.appearance["issues"][0]["code"],
                        "cad_appearance_render_identity_unresolved",
                    )

    def test_exact_document_material_is_not_overridden_by_legacy_texture(self):
        raw = _resolved_raw([None, None])
        raw["document"] = _layer(
            owner_configuration=CONFIGURATION,
            values=[
                0.0,
                1.0,
                0.0,
                1.0,
                1.0,
                0.5,
                0.3125,
                0.0,
                0.0,
            ],
            has_values=True,
            texture_state="present",
            texture_name="",
        )
        owner = _empty_state_owner(
            scope="component_document",
            document_path=DOCUMENT,
            configuration=CONFIGURATION,
            display_state="Display State 1",
        )
        owner["call_state"] = "observed"
        owner["native_render_material_count"] = 1
        binding_context = _binding_context(
            owner_scope="component_document",
            owner_document_path=DOCUMENT,
            api=(
                "IModelDoc2.GetPathName"
                "+ConfigurationManager.ActiveConfiguration.Name"
            ),
        )
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["document"],
                "entities_state": "observed",
                "entity_records": [
                    {
                        "entity_index": 0,
                        "native_object_type": "IModelDoc2",
                        "native_interface_kinds": ["IModelDoc2"],
                        "persistent_reference": None,
                        "binding_context": binding_context,
                        "native_identity": {
                            "kind": "document",
                            "qi_iid": "{B90793FB-EF3D-4B80-A5C4-99959CDB6CEB}",
                            "document_path": DOCUMENT,
                            "active_configuration": CONFIGURATION,
                            "owner_document_path": DOCUMENT,
                            "owner_configuration": CONFIGURATION,
                            "owner_display_state": "Display State 1",
                            "state": "observed",
                            "read_errors": {},
                        },
                        "read_errors": {},
                    }
                ],
                "primary_color": 0x000000FF,
                "transparency": 0.0,
                "color_form": 1,
                "texture_filename": "",
                "surface_finish_file": "",
                "read_errors": {},
            }
        ]
        raw["state_render_materials"] = [owner]
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": face_index,
                    "triangle_start": face_index,
                    "triangle_count": 1,
                    "face_reference_sha256": str(face_index + 1) * 64,
                }
                for face_index in (0, 1)
            ],
        )
        self.assertEqual(
            {face.rgba for face in resolved.faces},
            {(1.0, 0.0, 0.0, 1.0)},
        )
        for field, value in (
            ("qi_iid", "{wrong-iid}"),
            ("binding_api", "IModelDocExtension.GetCorrespondingEntity2"),
        ):
            malformed = copy.deepcopy(raw)
            entity = malformed["state_render_materials"][0]["materials"][
                0
            ]["entity_records"][0]
            if field == "qi_iid":
                entity["native_identity"]["qi_iid"] = value
            else:
                entity["binding_context"]["api"] = value
            with (
                self.subTest(field=field),
                self.assertRaises(AppearanceError) as caught,
            ):
                resolve_deliverable_appearance(
                    malformed,
                    component=COMPONENT,
                    expected_document_path=DOCUMENT,
                    expected_configuration=CONFIGURATION,
                    face_spans=[
                        {
                            "body_type": 0,
                            "body_index": 0,
                            "face_index": face_index,
                            "triangle_start": face_index,
                            "triangle_count": 1,
                            "face_reference_sha256": str(face_index + 1)
                            * 64,
                        }
                        for face_index in (0, 1)
                    ],
                )
            self.assertEqual(
                caught.exception.appearance["issues"][0]["code"],
                "cad_appearance_render_entity_binding_unproven",
            )

    def test_unproven_state_entity_binding_cannot_fall_back_to_document(self):
        for failure in (
            "persistent_reference",
            "unmapped_raw_relation",
            "source_claim_without_index",
            "wrong_owner",
            "conversion_in_owner",
            "missing_owner_reference",
            "binding_api_mismatch",
        ):
            with self.subTest(failure=failure):
                raw = _resolved_raw([None])
                raw["bodies"][0]["faces"][0]["appearance"] = _layer(
                    owner_configuration=CONFIGURATION
                )
                raw["bodies"][0]["appearance"] = _body_layer(
                    active_configuration=CONFIGURATION
                )
                raw["document"] = _layer(
                    owner_configuration=CONFIGURATION,
                    values=[
                        0.0,
                        0.0,
                        1.0,
                        1.0,
                        1.0,
                        0.5,
                        0.3125,
                        0.0,
                        0.0,
                    ],
                    has_values=True,
                )
                owner = _empty_state_owner(
                    scope="component_document",
                    document_path=DOCUMENT,
                    configuration=CONFIGURATION,
                    display_state="Display State 1",
                )
                owner["call_state"] = "observed"
                owner["native_render_material_count"] = 1
                entity = _state_entity_record(
                    kind="object",
                    reference=(1, 2, 3),
                )
                if failure == "persistent_reference":
                    entity["persistent_reference"] = None
                    entity["read_errors"] = {
                        "persistent_reference": {
                            "code": "cad_appearance_persistent_reference_missing"
                        }
                    }
                elif failure == "unmapped_raw_relation":
                    entity["persistent_reference"] = [9, 9, 9]
                elif failure == "source_claim_without_index":
                    entity["matched_identity"] = {
                        "occurrences": [],
                        "bodies": [],
                        "faces": [
                            {
                                "component": COMPONENT,
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                            }
                        ],
                        "features": [],
                    }
                elif failure == "wrong_owner":
                    entity["binding_context"]["component"] = "other-1"
                elif failure == "conversion_in_owner":
                    entity["binding_context"]["api"] = (
                        "IComponent2.GetCorresponding"
                    )
                elif failure == "missing_owner_reference":
                    entity["persistent_reference"] = None
                elif failure == "binding_api_mismatch":
                    entity["binding_context"]["api"] = (
                        "IModelDoc2.GetPathName"
                    )
                owner["materials"] = [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Display State 1"],
                        "entities": ["unbound-entity"],
                        "entities_state": "observed",
                        "entity_records": [entity],
                        "primary_color": 0x00AABBCC,
                        "transparency": 0.0,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ]
                raw["state_render_materials"] = [owner]
                with self.assertRaises(AppearanceError) as caught:
                    resolve_deliverable_appearance(
                        raw,
                        component=COMPONENT,
                        expected_document_path=DOCUMENT,
                        expected_configuration=CONFIGURATION,
                        face_spans=[
                            {
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                                "triangle_start": 0,
                                "triangle_count": 1,
                                "face_reference_sha256": "a" * 64,
                            }
                        ],
                    )
                self.assertEqual(
                    caught.exception.appearance["issues"][0]["code"],
                    "cad_appearance_render_entity_binding_unproven",
                )
    def test_source_render_material_uses_colorref_decode(self):
        raw = _resolved_raw(
            [[1.0, 0.0, 0.0, 1.0, 1.0, 0.5, 0.3125, 0.0, 0.0]]
        )
        raw["state_render_materials"] = [
            {
                "owner_document_path": DOCUMENT,
                "owner_scope": "component_document",
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "configuration": CONFIGURATION,
                "display_state": "Display State 1",
                "display_states": None,
                "display_states_state": "not_applicable",
                "native_render_material_count_api": (
                    "IModelDocExtension.GetRenderMaterialsCount2"
                ),
                "native_render_material_count": 1,
                "native_render_material_count_error": None,
                "call_state": "observed",
                "materials": [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Display State 1"],
                        "entities": ["native-face"],
                        "entities_state": "observed",
                        "entity_records": [
                            _state_entity_record(
                                kind="entity",
                                reference=(1, 2, 3),
                                native_interface_kinds=["IFace2", "IEntity"],
                            )
                        ],
                        "primary_color": 0x00AABBCC,
                        "transparency": 0.25,
                        "color_form": 1,
                        "texture_filename": "",
                        "surface_finish_file": "",
                        "read_errors": {},
                    }
                ],
                "mapping_errors": [],
                "native_reference_index": [],
                "read_errors": {},
            }
        ]
        raw["bodies"][0]["faces"][0]["appearance"][
            "material_property_values"
        ] = None
        raw["bodies"][0]["faces"][0]["appearance"][
            "has_material_property_values"
        ] = False
        resolved = resolve_deliverable_appearance(
            raw,
            component=COMPONENT,
            expected_document_path=DOCUMENT,
            expected_configuration=CONFIGURATION,
            face_spans=[
                {
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 1,
                    "face_reference_sha256": "a" * 64,
                }
            ],
        )
        self.assertEqual(
            resolved.faces[0].rgba,
            (0xCC / 255, 0xBB / 255, 0xAA / 255, 0.75),
        )


if __name__ == "__main__":
    unittest.main()
