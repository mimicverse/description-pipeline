"""Appearance raw remains evidence until every delivered face can be represented."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.appearance import (
    AppearanceError,
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
        "native_render_material_count": 0,
        "native_render_material_count_error": None,
        "call_state": "empty",
        "materials": [],
        "mapping_errors": [],
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
            "cad_appearance_render_color_encoding_unproven",
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

    def test_unproven_state_entity_binding_cannot_fall_back_to_document(self):
        for failure in (
            "persistent_reference",
            "identity_map",
            "unmapped_target",
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

    def test_source_render_material_owner_shape_stops_before_unproven_color_decode(self):
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
                "read_errors": {},
            }
        ]
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
            "cad_appearance_render_color_encoding_unproven",
        )


if __name__ == "__main__":
    unittest.main()
