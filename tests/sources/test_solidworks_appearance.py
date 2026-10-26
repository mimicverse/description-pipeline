"""Appearance raw remains evidence until every delivered face can be represented."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.appearance import (
    AppearanceError,
    _rgba_from_colorref,
    appearance_groups,
    bind_capture_appearance,
    resolve_deliverable_appearance,
)
from description_pipeline.geometry.native_tessellation import native_stl_subset

from . import support

COMPONENT = "arm-1"
DOCUMENT = r"C:\capture\arm.SLDPRT"
CONFIGURATION = "Painted"


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
                "enumeration_api": "IComponent2.GetBodies2",
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
    api: str = "IComponent2.GetCorrespondingEntity",
    reference_api: str | None = "IModelDocExtension.GetPersistReference3",
):
    return {
        "owner_scope": owner_scope,
        "owner_document_path": owner_document_path,
        "component": COMPONENT,
        "api": api,
        "reference_api": reference_api,
    }


def _native_identity(kind: str, state: str = "document"):
    return {
        "kind": kind,
        "state": "observed",
        "source_context": {
            "api": "test_native_context",
            "state": state,
            "component": COMPONENT,
            "read_errors": {},
        },
        "read_errors": {},
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
    return {
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
                "enumeration_api": "IComponent2.GetBodies2",
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
            {
                "persistent_reference": [4, 5, 6],
                "binding_context": _binding_context(
                    owner_scope="top_assembly",
                    owner_document_path=r"C:\capture\robot.SLDASM",
                    api="IModelDocExtension.GetPersistReference3",
                ),
                "target": {
                    "kind": "occurrence",
                    "component": COMPONENT,
                },
            }
        ]
        top["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Top State"],
                "entities": ["top-face"],
                "entities_state": "observed",
                "entity_records": [
                    {
                        "entity_index": 0,
                        "native_object_type": "IFace2",
                        "persistent_reference": [4, 5, 6],
                        "binding_context": _binding_context(
                            owner_scope="top_assembly",
                            owner_document_path=r"C:\capture\robot.SLDASM",
                            api="IModelDocExtension.GetPersistReference3",
                        ),
                        "native_identity": _native_identity(
                            "object",
                            state="assembly",
                        ),
                        "matched_identity": {
                            "occurrences": [],
                            "bodies": [],
                            "faces": [],
                            "features": [],
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
        owner["native_reference_index"] = [
            {
                "persistent_reference": [1, 2, 3],
                "binding_context": _binding_context(
                    owner_scope="component_document",
                    owner_document_path=DOCUMENT,
                    api="IComponent2.GetCorresponding",
                ),
                "target": {"kind": "occurrence", "component": COMPONENT},
            }
        ]
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["entity"],
                "entities_state": "observed",
                "entity_records": [
                    {
                        "entity_index": 0,
                        "native_object_type": "_CDispatch",
                        "persistent_reference": [1, 2, 3],
                        "binding_context": _binding_context(
                            owner_scope="component_document",
                            owner_document_path=DOCUMENT,
                            api="IComponent2.GetCorresponding",
                        ),
                        "native_identity": _native_identity("object"),
                        "matched_identity": {},
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
        owner["native_reference_index"] = [
            {
                "persistent_reference": [1, 2, 3],
                "binding_context": _binding_context(
                    owner_scope="component_document",
                    owner_document_path=DOCUMENT,
                    api="IComponent2.GetCorrespondingEntity",
                ),
                "target": {
                    "kind": "face",
                    "component": COMPONENT,
                    "body_type": 0,
                    "body_index": 0,
                    "face_index": face_index,
                },
            }
            for face_index in (0, 1)
        ]
        owner["materials"] = [
            {
                "material_index": 0,
                "linked_display_states": ["Display State 1"],
                "entities": ["ambiguous"],
                "entities_state": "observed",
                "entity_records": [
                    {
                        "entity_index": 0,
                        "native_object_type": "_CDispatch",
                        "persistent_reference": [1, 2, 3],
                        "binding_context": _binding_context(
                            owner_scope="component_document",
                            owner_document_path=DOCUMENT,
                            api="IComponent2.GetCorrespondingEntity",
                        ),
                        "native_identity": _native_identity("entity"),
                        "matched_identity": {},
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
                binding_context = _binding_context(
                    owner_scope="component_document",
                    owner_document_path=DOCUMENT,
                    api="IComponent2.GetCorresponding",
                )
                feature_identities = (
                    [(feature_names[0], [0, 1])]
                    if should_pass
                    else [("Boss-Extrude1", [0]), ("Cut-Extrude1", [1])]
                )
                owner["native_reference_index"] = [
                    {
                        "persistent_reference": [1, 2, 3],
                        "binding_context": binding_context,
                        "target": {
                            "kind": "feature",
                            "component": COMPONENT,
                            "name": feature_name,
                            "type_name": "Extrusion",
                            "persistent_reference": [4, 5, 6 + index],
                            "faces": [
                                {
                                    "body_type": 0,
                                    "body_index": 0,
                                    "face_index": face_index,
                                }
                                for face_index in face_indices
                            ],
                        },
                    }
                    for index, (feature_name, face_indices) in enumerate(
                        feature_identities
                    )
                ]
                owner["materials"] = [
                    {
                        "material_index": 0,
                        "linked_display_states": ["Display State 1"],
                        "entities": ["feature"],
                        "entities_state": "observed",
                        "entity_records": [
                            {
                                "entity_index": 0,
                                "native_object_type": "_CDispatch",
                                "persistent_reference": [1, 2, 3],
                                "binding_context": binding_context,
                                "native_identity": _native_identity("object"),
                                "matched_identity": {},
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
                        "cad_appearance_render_entity_ambiguous",
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
            owner_document_path=r"C:\capture\parent.SLDASM",
            api=(
                "IModelDoc2.GetPathName"
                "+ConfigurationManager.ActiveConfiguration.Name"
            ),
            reference_api=None,
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
                            "source_context": {
                                "api": "IModelDoc2",
                                "state": "document",
                                "component": COMPONENT,
                                "read_errors": {},
                            },
                            "read_errors": {},
                        },
                        "matched_identity": {},
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
            "identity_map",
            "unmapped_target",
            "source_claim_without_index",
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
                entity = {
                    "entity_index": 0,
                    "native_object_type": "_CDispatch",
                    "persistent_reference": [1, 2, 3],
                    "binding_context": _binding_context(
                        owner_scope="component_document",
                        owner_document_path=DOCUMENT,
                        api="IComponent2.GetCorresponding",
                    ),
                    "native_identity": _native_identity("object"),
                    "matched_identity": {
                        "occurrences": [],
                        "bodies": [],
                        "faces": [],
                        "features": [],
                    },
                    "read_errors": {},
                }
                if failure == "persistent_reference":
                    entity["persistent_reference"] = None
                    entity["read_errors"] = {
                        "persistent_reference": {
                            "code": "cad_appearance_persistent_reference_missing"
                        }
                    }
                elif failure == "identity_map":
                    owner["mapping_errors"] = [
                        {
                            "entity_kind": "faces",
                            "identity": {
                                "component": COMPONENT,
                                "body_type": 0,
                                "body_index": 0,
                                "face_index": 0,
                            },
                            "error": {
                                "code": "cad_appearance_persistent_reference_invalid"
                            },
                        }
                    ]
                elif failure == "source_claim_without_index":
                    entity["matched_identity"]["faces"] = [
                        {
                            "component": COMPONENT,
                            "body_type": 0,
                            "body_index": 0,
                            "face_index": 0,
                        }
                    ]
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
                            {
                                "entity_index": 0,
                                "native_object_type": "IFace2",
                                "persistent_reference": [1, 2, 3],
                                "binding_context": _binding_context(
                                    owner_scope="component_document",
                                    owner_document_path=DOCUMENT,
                                    api="IComponent2.GetCorrespondingEntity",
                                ),
                                "native_identity": _native_identity("entity"),
                                "read_errors": {},
                                "matched_identity": {
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
                                },
                            }
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
                "native_reference_index": [
                    {
                        "persistent_reference": [1, 2, 3],
                        "binding_context": _binding_context(
                            owner_scope="component_document",
                            owner_document_path=DOCUMENT,
                            api="IComponent2.GetCorrespondingEntity",
                        ),
                        "target": {
                            "kind": "occurrence",
                            "component": COMPONENT,
                        },
                    }
                ],
                "read_errors": {},
            }
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
            (0xCC / 255, 0xBB / 255, 0xAA / 255, 0.75),
        )


if __name__ == "__main__":
    unittest.main()
